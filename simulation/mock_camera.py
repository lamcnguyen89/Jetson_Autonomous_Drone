"""Synthetic perception for flight-behavior tests, independent of inference hardware.

A virtual person's GPS position is compared with the vehicle's position/yaw.
Field-of-view and distance limits decide whether Core sees a Detection. The
result uses the same camera-space X/Z meter convention as the real adapters,
but contains no image-derived depth, detector noise or actual neural inference.
"""

from camera.base import BaseCamera
from camera.detection import Detection
from .constants import MOCK_FOV, RADIUS_OF_EARTH, MOCK_Z_MAX, MOCK_FPS

import gpxpy
from gpxpy.gpx import GPXTrackPoint
import time
import threading
import math

STOP = False

def gpxToUnix(time):
    return time.timestamp() * 1000

def interpolate(currentPoint, nextPoint, offsetSinceCurrent):
    percent = (gpxToUnix(nextPoint.time) - gpxToUnix(currentPoint.time)) / offsetSinceCurrent

    deltaLat = nextPoint.latitude - currentPoint.latitude
    deltaLon =  nextPoint.longitude - currentPoint.longtude

    latInter = (currentPoint.latitude + deltaLat) * percent
    lonInter = (currentPoint.longtude + deltaLon) * percent

    return GPXTrackPoint(latInter, lonInter)

def thread(gpx, callback):
    global STOP
    points = gpx.tracks[0].segments[0].points

    loopStartTime = time.time() * 1000
    gpxStartTime = gpxToUnix(points[0].time)

    while STOP == False:
        offsetSinceStart = (time.time() * 1000) - loopStartTime

        # Find the point that is just after this offset
        currentPoint = None
        for point in points:
            timestamp = gpxToUnix(point.time)

            if timestamp <= gpxStartTime + offsetSinceStart:
                currentPoint = point
                break

        # If not the last point, pause a second
        # otherwise, close out the loop
        if points.index(currentPoint) == len(points) - 1:
            callback(currentPoint)
            print('DEBUG :: Finished GPX track, exiting loop')
            break
        else:
            # Iterpolate between this point and the next, given time delta
            nextPoint = points[points.index(currentPoint) + 1]
            interpolated = interpolate(currentPoint, nextPoint, offsetSinceStart - gpxToUnix(currentPoint.time))

            callback(interpolated)

            # Sleep needs to match FPS
            time.sleep(1.0 / float(MOCK_FPS))

class MockCamera(BaseCamera):
    '''
    A mocked camera implementation for usage in testing. It plays back
    a GPX file as a set of detections in relation to the SITL drone's
    reference frame
    '''

    gpxThread = None
    vehicle   = None
    _detections = []

    mockedPosition = {
        "latitude": 0,
        "longitude": 0
    }

    def __init__(self, vehicle):
        self.vehicle = vehicle

    def running(self):
        # This mock represents an always-available sensor; real camera workers
        # additionally check errors, shutdown and measurement freshness.
        return True

    def playbackGPXFromFile(self, filepath):
        file = open(filepath, 'r')
        if file == None:
            raise 'Cannot open GPX file at path: ' + filepath

        gpx = gpxpy.parse(file)

        self.gpxThread = threading.Thread(target=thread, args=(gpx, self._gpxThreadCallback))
        self.gpxThread.start()

    def _gpxThreadCallback(self, point):
        self.setGlobalCoordinate(point.latitude, point.longitude)

    def setGlobalCoordinate(self, latitude, longitude):
        '''
        Updates the camera's location to be the global coordinate, by
        generating a new detection that is in relation to the virtual
        drone.
        '''

        self.mockedPosition = {
            "latitude": latitude,
            "longitude": longitude
        }

        vehicleGlobalFrame = self.vehicle.location.global_relative_frame
        vehicleLatitude  = vehicleGlobalFrame.lat
        vehicleLongitude = vehicleGlobalFrame.lon
        vehicleHeading   = self.vehicleYaw()

        # Get normalised bearing
        newHeading = self.headingBetween(vehicleLatitude, vehicleLongitude, latitude, longitude)
        headingDiff = self.headingDiff(vehicleHeading, newHeading)
        angle = abs(headingDiff)
        isLeftward = headingDiff < 0

        if angle >= MOCK_FOV / 2:
            # Outside the vehicle's FOV, not a detection
            self._detections = []
            # print('DEBUG :: Outside FOV, no detections')
            return

        # Get distance between positions
        distance = self.distanceBetween(latitude, longitude, vehicleLatitude, vehicleLongitude)

        if distance >= MOCK_Z_MAX:
            # Too far away to be counted as a detection
            self._detections = []
            # print('DEBUG :: Beyond max distance, no detections')
            return

        # Resolve geographic range into camera-right X and camera-forward Z.
        # The 2D simulation fixes Y at zero; this is not an altitude measurement.
        xDistance = math.sin(math.radians(angle)) * distance
        zDistance = math.cos(math.radians(angle)) * distance

        if isLeftward:
            xDistance = 0.0 - xDistance

        detection = Detection(xDistance, 0.0, zDistance, 1.0, MOCK_FPS)
        self._detections = [detection]

        # print('DEBUG :: new mock detection. x: ' + str(xDistance) + ', z: ' + str(zDistance) + ', bearingDifference: ' + str(angle) + ', newHeading: ' + str(newHeading))

    def stop(self):
        global STOP

        STOP = True

        if self.gpxThread:
            self.gpxThread.join()
            print('DEBUG :: Stopped GPX thread')

        super().stop()

    def detections(self):
        return self._detections

    def headingBetween(self, lat1, lon1, lat2, lon2):
        # https://stackoverflow.com/a/17662363
        dLon = lon2 - lon1
        y = math.sin(dLon) * math.cos(lat2)
        x = (math.cos(lat1) * math.sin(lat2)) - (math.sin(lat1) * math.cos(lat2) * math.cos(dLon))

        bearing = math.degrees(math.atan2(y, x))

        # Normalise to compass (negative is to the right)
        if bearing < 0:
            bearing = abs(bearing)
        elif bearing > 0:
            bearing = 360 - bearing

        return bearing

    def vehicleYaw(self):
        yaw = math.degrees(self.vehicle.attitude.yaw)

        if yaw > 360:
            yaw -= 360
        elif yaw < 0:
            yaw = 360 + yaw

        return yaw

    def distanceBetween(self, lat1, lon1, lat2, lon2):
        lat1 = math.radians(lat1)
        lat2 = math.radians(lat2)
        lon1 = math.radians(lon1)
        lon2 = math.radians(lon2)
        dLat = lat2 - lat1
        dLon = lon2 - lon1

        a = math.sin(0.5 * dLat)**2 + math.sin(0.5 * dLon)**2 * math.cos(lat1) * math.cos(lat2)
        c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
        return RADIUS_OF_EARTH * c

    def headingDiff(self, h1, h2) -> int:
        '''
        The difference of two headings in degrees such that it is always in the range
        (-180, 180]. A negative number indicates [h2] is to the left of [h1].
        '''

        left = h1 - h2
        right = h2 - h1

        if left < 0: left += 360
        if right < 0: right += 360

        return -left if left < right else right
