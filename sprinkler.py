#!./venv/bin/python3

#set tabstop=4 expandtab:

# Import necessary modules
try:        
    import RPi.GPIO as GPIO  # For controlling GPIO pins on Raspberry Pi
except Exception as e:
    print("Not on the PI:", e)  # Handle cases where the code is not running on a Raspberry Pi
from pytz import timezone  # For timezone handling
import sys
import argparse  # For parsing command-line arguments
import yaml  # For reading YAML configuration files
import time
import threading
import concurrent.futures
from apscheduler.schedulers.background import BackgroundScheduler  # For scheduling tasks
import datetime
import os.path
import socketserver  # For creating a TCP server
from tendo import singleton  # To ensure only one instance of the program runs
import queue  # For thread-safe message queues
from flask import Flask, render_template, request, redirect, url_for, jsonify  # For creating a web interface
from io import StringIO  # For in-memory string handling
import re  # For regular expressions

# Set timezone to Mountain Time
mst = timezone('US/Mountain')

# Constants for sprinkler states
SPRINKLER_ON = 0
SPRINKLER_OFF = 1
SPRINKLER_UNKNOWN = -1  # Indicates uninitialized state

# Global variables
sprinklerThread = None  # Thread for the main routine
logname = "/home/pi/sprinkler.log"  # Log file path

# Parse command-line arguments
parser = argparse.ArgumentParser(description='Lawn Sprinkler Server')
parser.add_argument('-c', '--config', dest='yamlConfig', required=True,
                    help='Yaml configuration describing yard and zone setup.')
args = parser.parse_args()

# Initialize GPIO mode
try:
    GPIO.setmode(GPIO.BCM)  # Use Broadcom pin numbering
except Exception as e:
    print("WARN: GPIO.setmode is not configured properly:", e)

# Initialize Flask app
app = Flask(__name__)

# Sprinkler class to represent individual sprinklers
class Sprinkler:
    """Represents a single sprinkler zone."""
    sprinklerCount = 0

    def __init__(self, name, pin, description, hidden):
        self.name = name
        self.pin = pin
        self.state = SPRINKLER_UNKNOWN
        self.semaphore = threading.BoundedSemaphore(1)
        self.description = description
        # Configure GPIO pin
        try:
            GPIO.setup(pin, GPIO.OUT)
        except Exception as e:
            printl(f"WARN: GPIO is not configured properly: {e}")
        self.Off()
        if not hidden:
            Sprinkler.sprinklerCount += 1

    def displayCount(self):
        printl(f"Total Sprinklers {Sprinkler.sprinklerCount}")

    def displaySprinkler(self):
        with self.semaphore:
            printl(f"Name: {self.name}, pin: {self.pin}, description: {self.description}, state: {self.state}")

    def GetDataHash(self):
        """Return a dictionary with sprinkler details."""
        with self.semaphore:
            return {"name": self.name, "pin": self.pin, "description": self.description, "state": self.state}

    def GetState(self):
        """Return the current state of the sprinkler."""
        with self.semaphore:
            return self.state

    def On(self):
        """Turn on the sprinkler."""
        printl(f"INFO: Turning on sprinkler: {self.name}")
        with self.semaphore:
            try:
                GPIO.output(self.pin, SPRINKLER_ON)
                self.state = SPRINKLER_ON
            except Exception as e:
                printl(f"WARN: GPIO is not configured properly: {e}")

    def Off(self):
        """Turn off the sprinkler."""
        with self.semaphore:
            printl(f"INFO: Turning off sprinkler: {self.name}")
            try:
                GPIO.output(self.pin, SPRINKLER_OFF)
                self.state = SPRINKLER_OFF
            except Exception as e:
                printl(f"WARN: GPIO is not configured properly: {e}")

###############################################################################

# TCP handler for managing incoming and outgoing messages
class MyTCPHandler(socketserver.StreamRequestHandler):
    """
    The RequestHandler class for our server.
    """

    def handle(self):
        # Handle incoming messages from the client
        self.data = self.rfile.readline().strip()
        self.server.incomingMessageQueue.put(self.data)
        timeout = 5
        while timeout > 0 and self.server.outgoingMessageQueue.empty():
            timeout -= 1
            time.sleep(1)
        if not self.server.outgoingMessageQueue.empty():
            self.wfile.write(self.server.outgoingMessageQueue.get_nowait().encode())  # Send response to client
        else:
            self.wfile.write(f"Message: {self.data.decode()} received. No other messages received in time, closing connection.".encode())

###############################################################################

# Lawn class to manage all sprinklers and schedules
class Lawn:     
    """Manages all sprinklers and schedules."""
    def __init__(self):
        # Initialize the scheduler
        self.scheduler = BackgroundScheduler({
            'apscheduler.executors.default': {'class': 'apscheduler.executors.pool:ThreadPoolExecutor', 'max_workers': '1'},
            'apscheduler.executors.processpool': {'type': 'processpool', 'max_workers': '1'},
            'apscheduler.job_defaults.coalesce': 'false',
            'apscheduler.job_defaults.max_instances': '1',
            'apscheduler.timezone': mst
        })
        self.scheduler.start()
        self.server = None
        self.serverThread = None
        self.sprinklers = None
        self.incomingMessageQueue = queue.Queue()  # Queue for incoming messages
        self.outgoingMessageQueue = queue.Queue()  # Queue for outgoing messages
        self.stopEvent = threading.Event()  # Event to signal stopping of sprinkler jobs

    def __del__(self):
        # Shutdown the scheduler when the object is deleted
        self.scheduler.shutdown()

    def TurnOffAllSprinklers(self):
        """Turn off all sprinklers."""
        if self.sprinklers is None:
            return
        for zone in self.sprinklers:
            self.sprinklers[zone].Off()

    def GetSocketData(self):
        # Retrieve data from the incoming message queue
        if self.incomingMessageQueue.empty():
            return None
        else:
            return self.ParseMessage(self.incomingMessageQueue.get_nowait())

    def GetSprinklerState(self, zone):
        # Get the state of a specific sprinkler zone
        if zone in self.sprinklers:
          return self.sprinklers[zone].GetState()
        else: 
          printl("Zone: ", zone, " is not a recognized sprinkler zone")
          return SPRINKLER_UNKNOWN

    def GetAllSprinklers(self):
        """Return a dict of all sprinklers with their next scheduled runtime."""
        if self.sprinklers is None:
            printl("WARN: No sprinklers defined for lawn.")
            return {}
        mysprinklers = {}

        # Assign next runtime from jobs
        for job in self.scheduler.get_jobs():
            printl(f"Processing job: {job.name}")
            # Extract the zone list from the job name using regex
            match = re.search(r'zone\[(.*?)\]', job.name)
            if match:
                zone_list = [f"zone{z.strip()}" for z in match.group(1).split(",")]
                for zone in zone_list:
                    if zone in self.sprinklers:
                        sprinkler_data = self.sprinklers[zone].GetDataHash()
                        # Set the next runtime for the zone
                        sprinkler_data["next_runtime"] = job.next_run_time.strftime('%A %m-%d %I:%M%p') if job.next_run_time else "HERE"
                        mysprinklers[zone] = sprinkler_data
                    else:
                        printl(f"Zone {zone} in job {job.name} is not recognized.")

        # Add zones not in any job
        for zone in self.sprinklers:
            if zone not in mysprinklers:
                sprinkler_data = self.sprinklers[zone].GetDataHash()
                sprinkler_data["next_runtime"] = "No Schedule"
                mysprinklers[zone] = sprinkler_data

        return mysprinklers

    def ParseMessage(self, message):
        #printl("Received: " + message)
        # ON ZONE1 DURATION
        # OFF
        manualEventRunning = 0
        words = message.split(" ")
        if words[0]=="ON":
            if len(words) == 3:
                zone = words[1]
                dur_minutes = int(words[2])
                if zone in self.sprinklers:
                    zone = zone[4:]            
                    if  dur_minutes > 0 and dur_minutes < 61:
                        logMessage = "Scheduling: " + message
                        self.outgoingMessageQueue.put(logMessage)
                        manualEventRunning = 1
                        self.RunEvent("ManualCall", [zone], dur_minutes)
                    else:
                        logMessage = f"Duration out of range 0<dur<30min: {dur_minutes}"
                else:
                    logMessage = "Zone unrecognized: " + zone                
            else:
                logMessage = f"Recieved badly formatted ON message: '{message}'"
        elif words[0]=="OFF":
            logMessage = "Message Received: All Off"
            self.TurnOffAllSprinklers()
        else:
            logMessage = f"Message not recognized: '{message}'"
        printl(logMessage)
        if not manualEventRunning:
            self.outgoingMessageQueue.put(logMessage)
        return None

    def StartServer(self, host, port):
        try:
            printl("Starting server")
            self.server = socketserver.TCPServer((host, port), MyTCPHandler)  # Updated from SocketServer
            #Use the same queue to communicate between lawn and TcpServer
            self.server.incomingMessageQueue = self.incomingMessageQueue
            self.server.outgoingMessageQueue = self.outgoingMessageQueue
            printl(f"Creating thread to handle server")
            self.serverThread = threading.Thread(target=self.server.serve_forever)
            self.serverThread.setDaemon(True) #don't hang on exit
            printl(f"Starting server thread")
            self.serverThread.start()
            printl(f"Server up and running")
        except:
            printl(f"Error: Problem starting server")
            pass

    def EraseEventQueue(self):
        printl(f"Erasing all previously scheduled events.")
        for job in self.scheduler.get_jobs():
            job.remove()
        return

    def NotifyOwner(self, message):
        self.TurnOffAllSprinklers()
        printl(f"{message}")

    def RunEvent(self, schedName, zones, duration):
        """Run a scheduled or manual event for a list of zones."""
        self.TurnOffAllSprinklers()
        self.stopEvent.clear()
        try:
            printl(f"Running event {schedName} on zones: {', '.join(map(str, zones))}")
            for zone in zones:
                if self.stopEvent.is_set():
                    printl("Stop event detected, ending job early.")
                    break
                printl(f"Starting sprinklers in zone: {zone} Duration: {duration} minutes.")
                self.sprinklers[f"zone{zone}"].On()
                for _ in range(duration * 60):
                    if self.stopEvent.is_set():
                        printl("Stop event detected, turning off current zone.")
                        break
                    time.sleep(1)
                    self.GetSocketData()
                printl(f"Stopping sprinklers in zone: {zone}")
                self.sprinklers[f"zone{zone}"].Off()
            printl(f"Finished event {schedName} on zones: {', '.join(map(str, zones))}")
        finally:
            pass

    #Once an event is scheduled that event is responsible for 
    # scheduling its next event
    def ScheduleAllEvents(self, yamlSchedule):
        if yamlSchedule == None:
            return
        for schedName in yamlSchedule: 
            self.ScheduleOneEvent(schedName, yamlSchedule[schedName]['cron'], yamlSchedule[schedName]['zones'], yamlSchedule[schedName]['duration'])
        self.scheduler.print_jobs()
        return

    def GetStatus(self):
        output = StringIO()
        self.scheduler.print_jobs(out=output)
        job_list = output.getvalue()
        output.close()
        print (job_list)
        return job_list
   
    def ScheduleOneEvent(self, schedName, cron, zones, duration):
        parts = cron.split(" ")
        if len(parts) != 6:
            self.NotifyOwner("Cron job does not have the proper number of fields: ", schedName, cron, zones)
        summary_string = f"{schedName} zone{zones} {cron} Duration: {duration}"
        printl("Scheduling Event: " + summary_string)
        self.scheduler.add_job(
            self.RunEvent,
            'cron',
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
            year=parts[5],
            args=[schedName, zones, duration],
            misfire_grace_time=None,
            max_instances=1,
            name=summary_string
        )

    def Configure(self, yamlConfig):
        printl(f"Loading configuration from: {yamlConfig}")
        try:
            with open(yamlConfig, 'r') as stream:
                configuration = yaml.load(stream, Loader=yaml.FullLoader)  # Use FullLoader for safe YAML parsing
                printl(f"Configuration loaded: {configuration}")
        except yaml.error.MarkedYAMLError as e:
            # Report the line and column of the YAML syntax error
            printl(f"YAML syntax error in file '{yamlConfig}' at line {e.problem_mark.line + 1}, column {e.problem_mark.column + 1}: {e.problem}")
            return
        except Exception as e:
            # Handle other exceptions
            printl(f"Error loading YAML configuration: {e}")
            return

        # Only configure sprinklers once
        if self.sprinklers is None:
            self.sprinklers = dict()
            for zone in configuration['lawn']:
                printl(f"Configuring zone: {zone}")
                if 'description' not in configuration['lawn'][zone].keys():
                    printl(f"INFO: Zone {zone} is missing description field.")
                    configuration['lawn'][zone]['description'] = ""
                if 'pin' not in configuration['lawn'][zone].keys():
                    raise Exception(f"Zone {zone} is missing required pin field.")
                if 'hidden' in configuration['lawn'][zone] and configuration['lawn'][zone]['hidden'] == 1:
                    # Don't save hidden sprinklers
                    unused = Sprinkler(zone, configuration['lawn'][zone]['pin'], configuration['lawn'][zone]['description'], 1)
                else:
                    self.sprinklers[zone] = Sprinkler(zone, configuration['lawn'][zone]['pin'], configuration['lawn'][zone]['description'], 0)
            if 'host' in configuration.keys() and 'port' in configuration.keys():
                self.StartServer(configuration['host'], configuration['port'])
            else:
                printl("No host and/or port given in YAML configuration. Add these keys and restart the program to start the server.")
        else:
            # Turn off all sprinklers to stabilize the system
            self.TurnOffAllSprinklers()

        # Clear out any events and populate new ones
        self.EraseEventQueue()
        self.ScheduleAllEvents(configuration['schedules'])
        printl("Configuration complete.")
        return


def printl(message):
    with open(logname, 'a') as logFile:
        now = datetime.datetime.now()
        print(f"{now}: {message}")
        logFile.write(f"{now}: {message}\n")

def EpochToTimeStamp(epoch):
    pattern = '%m/%d/%Y %H:%M'
    return time.strftime(pattern, time.localtime(epoch))

def main(yamlConfig, mylawn, isActiveEvent):
    #Ensure only one instance of the program runs
    me = singleton.SingleInstance()

    # Initialize log file
    logFile = open(logname, 'w')
    logFile.write("Beginning Sprinkler Routine:")
    logFile.close()

    # Main loop to monitor configuration changes and manage sprinklers
    oldTimeStamp = int(0)
    while 1:
        # Check for configuration updates
        modifiedTime = oldTimeStamp
        try:
            modifiedTime = os.path.getmtime(yamlConfig)
        except:
            print("OS problem, will try again soon...")
        if modifiedTime > oldTimeStamp:
            oldTimeStamp = modifiedTime
            printl("Config file has changed, updating schedules...")
            mylawn.Configure(yamlConfig)
            isActiveEvent.set()
        mylawn.GetSocketData()
        time.sleep(1)

# Flask route for the web interface
@app.route('/', methods=['GET', 'POST'])
def index():
    """Main web interface."""
    global args, sprinklerThread, mylawn
    if sprinklerThread is None:
        mylawn = Lawn()
        isActiveEvent = threading.Event()
        sprinklerThread = threading.Thread(target=main, args=[args.yamlConfig, mylawn, isActiveEvent])
        sprinklerThread.start()
        isActiveEvent.wait()
    sprinklers = {}
    schedule_status = ""
    if mylawn is not None:
        if mylawn.sprinklers is None:
            mylawn.Configure(args.yamlConfig)
        sprinklers = mylawn.GetAllSprinklers()
        schedule_status = mylawn.GetStatus()
    if request.method == 'POST':
        if 'submit' in request.form:
            if request.form['submit'][:4] == 'zone':
                duration = int(request.form['duration'])
                mylawn.RunEvent("Web Event", request.form['submit'][4], duration)
            elif request.form['submit'] == "All Off":
                mylawn.TurnOffAllSprinklers()
        return redirect(url_for('index'))
    return render_template('index.html', sprinklers=sprinklers, SPRINKLER_ON=SPRINKLER_ON, SPRINKLER_OFF=SPRINKLER_OFF, SPRINKLER_UNKNOWN=SPRINKLER_UNKNOWN, schedule_status=schedule_status)

@app.route('/control', methods=['POST'])
def control():
    """AJAX endpoint to control individual zones or all zones."""
    zone = request.args.get('zone')
    action = request.args.get('action')
    printl(f"Control request: zone={zone}, action={action}")
    if action == 'on':
        mylawn.RunEvent("Web Event", [zone], 15)
    elif action == 'off':
        if zone == 'All':
            mylawn.stopEvent.set()
            mylawn.TurnOffAllSprinklers()
        else:
            mylawn.TurnOffZone(zone)
    return jsonify({"status": "success", "zone": zone, "action": action})

@app.route('/run_zones', methods=['POST'])
def run_zones():
    """AJAX endpoint to run a series of zones for a given duration."""
    data = request.get_json()
    zones_str = data.get('zones', '')
    duration = int(data.get('duration', 10))
    zones = [z.strip() for z in zones_str.split(',') if z.strip()]
    if not zones:
        return jsonify({"status": "No zones specified"}), 400
    printl(f"Running zones in series: {zones} for {duration} minutes each")
    mylawn.RunEvent("Web RunZones", zones, duration)
    return jsonify({"status": f"Started zones {', '.join(zones)} for {duration} minutes each"})

