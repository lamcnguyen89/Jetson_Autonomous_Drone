"""Local WebSocket transport between the simulator and its browser visualiser.

The socket/event loop runs in its own thread. Simulation code queues updates
and registers a callback for incoming UI controls; the UI does not connect to
the real camera or the physical MAVLink serial port through this class.
"""

import asyncio
import websockets
import time
import threading
import json

def UIThread(connection):
    """Run the websocket service loop for one simulation UI connection."""
    connection.run()

class UIConnection:
    """Bridge websocket messages between the browser visualizer and simulation."""

    _sendQueue = []
    _lock = asyncio.Lock()
    _active = False

    _onDataCallback = None

    def __init__(self):
        """Initialize connection state and an empty callback for inbound UI data."""
        thread = threading.Thread(target=UIThread, args=(self,))
        thread.start()

        self._active = True

    async def msgHandler(self, websocket, path):
        """Run the send and receive handlers for a newly connected browser client."""
        sendTask = asyncio.create_task(self.sendHandler(websocket, path))
        receiveTask = asyncio.create_task(self.receiveHandler(websocket, path))

        await asyncio.wait([sendTask, receiveTask], return_when=asyncio.FIRST_COMPLETED)

    async def sendHandler(self, websocket, _):
        """Continuously send queued simulation updates to the connected browser."""
        # Batch queued updates into the newline-delimited format consumed by
        # the visualiser, independently of Core's control-cycle frequency.
        while True:
            await asyncio.sleep(0.05)

            if len(self._sendQueue) == 0:
                continue

            message = '\n'.join(self._sendQueue)

            try:
                await websocket.send(message)
            except websockets.exceptions.ConnectionClosedOK:
                print('connection closed during send')

            self._sendQueue = []

    async def receiveHandler(self, websocket, _):
        """Receive browser messages and dispatch them to the registered callback."""
        async for message in websocket:
            await self.handleMessage(message)

    async def handleMessage(self, data: str):
        """Decode an incoming JSON message and invoke the application data callback."""
        if self._onDataCallback == None:
            return

        # newline split json. Convert to object then pass up-chain
        messages = data.split('\n')

        for message in messages:
            try:
                self._onDataCallback(json.loads(message))
            except Exception as e:
                print(e)

    async def main(self):
        """Start the websocket server and keep it serving until explicitly stopped."""
        self.stop_event = threading.Event()
        stop = asyncio.get_event_loop().run_in_executor(None, self.stop_event.wait)

        async with websockets.serve(lambda websocket, path: self.msgHandler(websocket, path), '127.0.0.1', 5678):
            await stop

    def run(self):
        """Launch the asyncio server loop on a dedicated thread."""
        asyncio.run(self.main())

    def stop(self):
        """Request server shutdown and close the active browser connection."""
        self.stop_event.set()
        self._active = False

    def active(self):
        """Report whether a browser websocket is currently connected."""
        return self._active

    def send(self, data):
        """Queue a simulation update for asynchronous delivery to the browser."""
        self._sendQueue.append(json.dumps(data))

    def setOnDataCallback(self, callback):
        """Register the function that receives decoded browser-to-simulation data."""
        self._onDataCallback = callback

if __name__ == '__main__':
    connection = UIConnection()

    while True:
        try:
            time.sleep(1)
        except:
            break

    connection.stop()
