#!/usr/bin/python

from boto import connect_s3, connect_ec2
from boto.s3.key import Key
from kombu import Exchange, Queue
from otpsetup.shortcuts import DjangoBrokerConnection
from otpsetup.shortcuts import stop_current_instance
from otpsetup.client.models import GtfsFile
from otpsetup import settings
from shutil import copyfileobj
from datetime import datetime

import os
import subprocess
import tempfile
import socket

import sys, traceback

exchange = Exchange("amq.direct", type="direct", durable=True)
queue = Queue("validate_request", exchange=exchange, routing_key="validate_request")

def s3_bucket(cache = {}):
    if not 'bucket' in cache:

        connection = connect_s3(settings.AWS_ACCESS_KEY_ID, settings.AWS_SECRET_KEY)
        bucket = connection.get_bucket(settings.S3_BUCKET)
        cache['bucket'] = bucket
    else:
        return cache['bucket']
    return bucket


def validate(conn, body, message):
    #download the GTFS files and run them through the feed validator
    try:
        #create a working directory for this feed
        #directory = tempfile.mkdtemp()
        now = datetime.now()
        directory = "/mnt/req%s_%s" % (body['request_id'], now.strftime("%F-%T"))
        os.makedirs(directory)
        files = body['files']
        out = []
        for s3_id in files:

            bucket = s3_bucket()
            key = Key(bucket)
            key.key = s3_id

            basename = os.path.basename(s3_id)
            path = os.path.join(directory, basename)

            key.get_contents_to_filename(path)
            result = subprocess.Popen(["/usr/local/bin/feedvalidator.py", "-n", "--output=CONSOLE", "-l", "10", path], stdout=subprocess.PIPE)
            out.append({"key" : s3_id, "errors" : result.stdout.read()})
            os.remove(path)

        os.rmdir(directory)
        publisher = conn.Producer(routing_key="validation_done",
                                  exchange=exchange)
        publisher.publish({'request_id' : body['request_id'], 'output' : out})
        message.ack()

    except:
        now = datetime.now()
        errfile = "/var/otp/val_err_%s_%s" % (body['request_id'], now.strftime("%F-%T"))
        traceback.print_exc(file=open(errfile,"a"))

with DjangoBrokerConnection() as conn:

    with conn.Consumer(queue, callbacks=[lambda body, message: validate(conn, body, message)]) as consumer:
        # Process messages and handle events on all channels

        print "starting loop"
        try:
            while True:
                conn.drain_events(timeout=300)
        except:
            print "exited loop"
    conn.close()

# stop this instance            
stop_current_instance()


