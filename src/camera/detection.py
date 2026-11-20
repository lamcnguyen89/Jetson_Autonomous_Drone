'''
A person position in the camera's reference frame, shared by both backends.

Only x/y/z are meters: X points right, Y up, Z forward. Confidence is a model
score and fps describes processing cadence. Optional track_id is a short-lived
association from the Jetson tracker, not a permanent identity for that person.
Keeping model-specific bounding boxes out of this object lets the flight rules
operate on physical distances with either inference processor.
'''
class Detection():

    x = 0
    y = 0
    z = 0
    confidence = 0
    fps = 0

    def __init__(self, x, y, z, confidence, fps, track_id=None):
        # The camera backend has already converted device depth to meters.

        self.x = x
        self.y = y
        self.z = z
        self.confidence = confidence
        self.fps = fps
        self.track_id = track_id
