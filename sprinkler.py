#!./venv/bin/python2.7

#set tabstop=4 expandtab:

try:        
    import RPi.GPIO as GPIO
except:
    print "Not on the PI"
from pytz import timezone
import sys
import argparse
import yaml
import time
import threading
import concurrent.futures
#from apscheduler.scheduler import Scheduler
from apscheduler.schedulers.background import BackgroundScheduler
import datetime
import os.path
import SocketServer
from tendo import singleton
import Queue
from flask import Flask, render_template, request, redirect, url_for
import StringIO

mst = timezone('US/Mountain')

#Circuit is active low
SPRINKLER_ON = 0
SPRINKLER_OFF = 1
#Indicates unknown on or off, unititialized
SPRINKLER_UNKNOWN = -1 

#Thread for main routine
#Global because their can only be one no matter how often web app is restarted
sprinklerThread = None
logname = "/home/pi/sprinkler.log"

parser = argparse.ArgumentParser(description='Lawn Sprinkler Server')
parser.add_argument('-c', '--config', dest='yamlConfig', required=1,
                    help='Yaml configuration describing yard and zone setup.')
args = parser.parse_args()

#All GPIO operations need to be in try catch blocks
try:
    GPIO.setmode(GPIO.BCM)
except:
    print "WARN: GPIO.setmode is not configured properly"

app = Flask(__name__)

class Sprinkler:
    'Common base class for all sprinklers'
    sprinklerCount = 0

    def __init__(self, name, pin, description, hidden):
        self.name = name
        self.pin = pin
        self.state = SPRINKLER_UNKNOWN
        self.semaphore = threading.BoundedSemaphore(1)        
        self.description = description
        #Immediately setup pin
        try:
            #print "Pin Name: " + str(pin)
            GPIO.setup(pin, GPIO.OUT)
        except:
            printl("WARN: GPIO is not configured properly")
        self.Off()
        if hidden:
          return
        Sprinkler.sprinklerCount += 1
   
    def displayCount(self):
        printl("Total Sprinklers %d" % Sprinkler.sprinklerCount)

    def displaySprinkler(self):
        self.semaphore.acquire()
        printl("Name: ", self.name,  ", pin: ", self.pin, ", description: ", self.description, " state: ", self.state)
        self.semaphore.release()

    def GetDataHash(self):
        self.semaphore.acquire()
        myhash = { "name": self.name, "pin": self.pin, "description": self.description, "state": self.state }
        self.semaphore.release()
        return myhash

    
    def GetState(self):
        self.semaphore.acquire()
        mystate = self.state
        self.semaphore.release()
        return mystate

    def On(self):
        printl("INFO: Turning on sprinkler: %s" % self.name)
        self.semaphore.acquire()
        try:
            GPIO.output(self.pin, SPRINKLER_ON)
            self.state = SPRINKLER_ON
        except:
            printl("WARN: GPIO is not configured properly")
        self.semaphore.release()

    def Off(self):
        self.semaphore.acquire()
        printl("INFO: Turning off sprinkler: %s" % self.name)
        try:
            GPIO.output(self.pin, SPRINKLER_OFF)
            self.state = SPRINKLER_OFF
        except:
            printl("WARN: GPIO is not configured properly")
        self.semaphore.release()

###############################################################################

class MyTCPHandler(SocketServer.StreamRequestHandler):
    """
    The RequestHandler class for our server.

    It is instantiated once per connection to the server, and must
    override the handle() method to implement communication to the
    client.
    """

    def handle(self):
        # self.rfile is a file-like object created by the handler;
        # we can now use e.g. readline() instead of raw recv() calls
        self.data = self.rfile.readline().strip()
        self.server.incomingMessageQueue.put(self.data)
        #print "{} wrote:".format(self.client_address[0])
        #print self.data
        # Likewise, self.wfile is a file-like object used to write back
        # to the client
        #self.wfile.write(self.data.upper())
        #self.wfile.write("Message: " + self.data + " received and added to message queue.")
        #Wait up to 5 seconds for a reponse message
        timeout = 5
        while timeout>0 and self.server.outgoingMessageQueue.empty():
            timeout=timeout - 1
            time.sleep(1)
        if not self.server.outgoingMessageQueue.empty():
            self.wfile.write(self.server.outgoingMessageQueue.get_nowait())
        else:
            self.wfile.write("Message: "+ self.data + " received. No other messages received in time, closing connection.")

###############################################################################
class Lawn:     
    def __init__(self):
        #Start a scheduler as a background thread
        #self.scheduler = BackgroundScheduler(timezone=mst)
#, executors.processpool={'type'='processpool', 'max_workers'
#='1'})
        self.scheduler = BackgroundScheduler({
           'apscheduler.executors.default': {
               'class': 'apscheduler.executors.pool:ThreadPoolExecutor',
               'max_workers': '1'
           },
           'apscheduler.executors.processpool': {
               'type': 'processpool',
               'max_workers': '1'
           },
           'apscheduler.job_defaults.coalesce': 'false',
           'apscheduler.job_defaults.max_instances': '1',
           'apscheduler.timezone': mst
        })
        self.scheduler.start()
        self.server       = None
        self.serverThread = None
        self.sprinklers   = None
        self.incomingMessageQueue = Queue.Queue()
        self.outgoingMessageQueue = Queue.Queue()
 
    def __del__(self):
        self.scheduler.shutdown()
        
    def TurnOffAllSprinklers(self):
        if self.sprinklers == None:
            return
        for zone in self.sprinklers:
            self.sprinklers[zone].Off()
        return

    def GetSocketData(self):
        if self.incomingMessageQueue.empty():
            return None
        else:
            return self.ParseMessage(self.incomingMessageQueue.get_nowait())

    def GetSprinklerState(self, zone):
        if zone in self.sprinklers:
          return self.sprinklers[zone].GetState()
        else: 
          printl("Zone: ", zone, " is not a recognized sprinkler zone")
          return SPRINKLER_UNKOWN

    def GetAllSprinklers(self):
        mysprinklers = {} 
        if self.sprinklers is None:
            printl("WARN: No sprinklers defined for lawn.")
        for zone in self.sprinklers:
            mysprinklers[zone] = self.sprinklers[zone].GetDataHash()
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
                        logMessage = "Duration out of range 0<dur<30min: " + str(dur_minutes)
                else:
                    logMessage = "Zone unrecognized: " + zone                
            else:
                logMessage = "Recieved badly formatted ON message: '" + message + "'"
        elif words[0]=="OFF":
            logMessage = "Message Received: All Off"
            self.TurnOffAllSprinklers()
        else:
            logMessage = "Message not recognized: '" + message + "'"
        printl(logMessage)
        if not manualEventRunning:
            self.outgoingMessageQueue.put(logMessage)
        return None

    def StartServer(self, host, port):
        try:
            printl("Starting server")
            self.server = SocketServer.TCPServer((host, port), MyTCPHandler)
            #Use the same queue to communicate between lawn and TcpServer
            self.server.incomingMessageQueue = self.incomingMessageQueue
            self.server.outgoingMessageQueue = self.outgoingMessageQueue
            printl("Creating thread to handle server")
            self.serverThread = threading.Thread(target=self.server.serve_forever)
            self.serverThread.setDaemon(True) #don't hang on exit
            printl("Starting server thread")
            self.serverThread.start()
            printl("Server up and running")
        except:
            printl("Error: Problem starting server")
            pass

    def EraseEventQueue(self):
        printl("Erasing all previously scheduled events.")
        for job in self.scheduler.get_jobs():
            job.remove()
        return

    def NotifyOwner(self, message):
        self.TurnOffAllSprinklers()
        printl(message)

    def RunEvent(self, schedName, zones, duration):
        self.TurnOffAllSprinklers()
        try:
            printl("Running event " + schedName + " on zones: " + ", " . join(map(str,zones)))
            #try:
            for zone in zones:
                printl("Starting sprinklers in zone: " + str(zone) + " Duration: " + str(duration) + " minutes.")
                self.sprinklers["zone"+str(zone)].On()
                for counter in range(0, duration*60, 1):
                    time.sleep(1)
                    self.GetSocketData()
                printl("Stopping sprinklers in zone: " + str(zone))
                self.sprinklers["zone"+str(zone)].Off()
            #except:
            #    self.NotifyOwner("Danger Danger: Something bad happened in RunEvent() shutting down")

            printl("Finished event " + schedName + " on zones: " + ", " . join(map(str,zones)))
        finally:
            pass

        return

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
        output = StringIO.StringIO()
        self.scheduler.print_jobs(out=output)
        job_list = output.getvalue()
        output.close()
        print job_list
        return job_list
   
    def ScheduleOneEvent(self, schedName, cron, zones, duration):
        parts = cron.split(" ")
        if (len(parts)!=6):
            self.NotifyOwner("Cron job does not have the proper number of fields: ", schedName, cron, zones )
        summary_string = schedName + " " + cron + " " + str(zones) + " Duration: " + str(duration)
        printl("Scheduling Event: " + summary_string)
        #self.scheduler.add_cron_job(self.RunEvent, minute=parts[0], hour=parts[1], day=parts[2], month=parts[3], day_of_week=parts[4], year=parts[5], args=[schedName, zones, duration])
        #misfire_grace_time = time in seconds where job is still allowed to run
        self.scheduler.add_job(self.RunEvent, 'cron', minute=parts[0], hour=parts[1], day=parts[2], month=parts[3], day_of_week=parts[4], year=parts[5], args=[schedName, zones, duration], misfire_grace_time=None,max_instances=1,name=summary_string)

    def Configure(self, yamlConfig):
        stream = open(yamlConfig, 'r')
        try:
            configuration = yaml.load(stream)
        except:
            printl("Yaml syntax error, please update config and try again. Not making any changes.")
            return
        #Only configure spriklers once
        #After that only allow schedule updates
        if self.sprinklers == None:
            self.sprinklers = dict()
            for zone in configuration['lawn']:
                if 'description' not in configuration['lawn'][zone].keys():
                    printl("INFO: Zone",zone,"is missing description field.")
                    configuration['lawn'][zone]['description'] = ""
                if 'pin' not in configuration['lawn'][zone].keys():
                    raise Exception("Zone",zone,"is missing required pin field.")
                    #print zone, configuration['lawn']['description'],"\n"
                if 'hidden' in configuration['lawn'][zone] and configuration['lawn'][zone]['hidden'] == 1:
                    #Dont save hidden sprinklers, they are just there for turning off the GPIO
                    unused = Sprinkler(zone, configuration['lawn'][zone]['pin'], configuration['lawn'][zone]['description'], 1)
                else:
                    self.sprinklers[zone] = Sprinkler(zone, configuration['lawn'][zone]['pin'], configuration['lawn'][zone]['description'], 0)
            if ('host' in configuration.keys()) and ('port' in configuration.keys()):
                self.StartServer(configuration['host'],configuration['port'])
            else: 
                printl("No host and/or port given in yaml configuration, add these keys and restart program to start server")
        else:
            #TurnOff All Sprinklers
            #Sprinklers start off, do this here to get the sytem in a stable state
            # when not starting from scratch
            self.TurnOffAllSprinklers()
        #Clear out any events, first time through this is empty
        self.EraseEventQueue()
        #Populate new events
        self.ScheduleAllEvents(configuration['schedules'])
        return


def printl(message):
    logFile = open(logname, 'a')
    now = datetime.datetime.now()
    print str(now) + ": " + message   
    logFile.write(str(now) + ": " + message + "\n")
    logFile.close()  

def EpochToTimeStamp(epoch):
    pattern = '%m/%d/%Y %H:%M'
    return time.strftime(pattern, time.localtime(epoch))

def main(yamlConfig, mylawn, isActiveEvent):
    #Only allow this program to run one instance
    #Multiple instances could damage the hardware
    me = singleton.SingleInstance()

    logFile = open(logname, 'w')
    logFile.write("Beginning Sprinkler Routine:")
    logFile.close()

    #Create a bogus old date to kickstart the scheduling
    oldTimeStamp = int(0)

    now = datetime.datetime.now()
    eventQueue = ()
    counter=0
    while 1:
        counter += 1
        #Periodically display status
        if counter>10:
            counter = 0
            #mylawn.GetStatus()
        now = datetime.datetime.now()
        #Check configs for new updates
        modifiedTime = oldTimeStamp
        try:
            modifiedTime = os.path.getmtime(yamlConfig)
        except:
            print "OS problem, will try again soon..."
        if modifiedTime > oldTimeStamp:
            oldTimeStamp = modifiedTime
            printl("Config file has changed, updating schedules...")
            #print str(oldTimeStamp) + "\n" + str(modifiedTime) + "\n" + str(now) 
            #Read Configuration and schedule events
            mylawn.Configure(yamlConfig)
            #Notify the parent thread that we are now up and running
            isActiveEvent.set()
        mylawn.GetSocketData()
        time.sleep(1)

@app.route('/', methods=['GET','POST'])
def index():
    global args
    global sprinklerThread
    global mylawn

    if sprinklerThread is None:
        mylawn = Lawn()
        isActiveEvent = threading.Event()
        sprinklerThread = threading.Thread(target=main, args=[args.yamlConfig, mylawn, isActiveEvent])
        sprinklerThread.start()
        isActiveEvent.wait()
    
    sprinklers = {}
    schedule_status = ""
    if mylawn is not None:
       print "Info: Collecting sprinkler data"
       sprinklers = mylawn.GetAllSprinklers()
       schedule_status = mylawn.GetStatus() 

    if request.method == 'POST':
        print "Incoming POST: ", request.data
        if request.form['submit'][:4] == 'zone':
            duration = int(request.form['duration'])
            print "Duration: ", duration
            mylawn.RunEvent("Web Event", request.form['submit'][4], duration)
        elif request.form['submit'] == "All Off":
            mylawn.TurnOffAllSprinklers()
        else:
            print "Unknown input", request.form['submit']
        return redirect(url_for('index'))
    elif request.method == 'GET':
        print "rendering"
    print schedule_status
    return render_template('index.html', sprinklers=sprinklers, SPRINKLER_ON=SPRINKLER_ON, SPRINKLER_OFF=SPRINKLER_OFF, SPRINKLER_UNKNOWN=SPRINKLER_UNKNOWN, schedule_status=schedule_status)

#@app.route('/_get_values')
#def GetValues():
#    global mylawn
#    sprinklers = {}
#    if mylawn is not None:
#       print "Info: Collecting sprinkler data"
#       sprinklers = mylawn.GetAllSprinklers()
#    return jsonify(sprinklers)
#render_template('index.html', sprinklers=sprinklers, SPRINKLER_ON=SPRINKLER_ON, SPRINKLER_OFF=SPRINKLER_OFF, SPRINKLER_UNKNOWN=SPRINKLER_UNKNOWN)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True, debug=False)
    
