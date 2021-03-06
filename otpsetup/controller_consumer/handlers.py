
#!/usr/bin/python

from boto import connect_ec2, connect_s3
from kombu import Exchange
from django.core.mail import send_mail
from django.core.urlresolvers import reverse
from otpsetup.client.models import InstanceRequest, GtfsFile, DeploymentHost, ManagedDeployment, GraphBuild, ManagedGtfsFeed, GtfsBuildMapping, BuildHostMapping
from otpsetup import settings

import base64, datetime, traceback

exchange = Exchange("amq.direct", type="direct", durable=True)

def validation_done(conn, body):

    request_id = body['request_id']
    output = body['output']

    for gtfs_result in output:
        gtfs_file = GtfsFile.objects.get(s3_key=gtfs_result['key'])
        gtfs_file.validation_output = gtfs_result['errors']
        gtfs_file.save()

    irequest = InstanceRequest.objects.get(id=request_id)
    irequest.state = "validate_done"
    irequest.save()

    #url = reverse("otpsetup.client.adminviews.index")

    send_mail('OTP instance request pending', 
              """There is a new OTP instance request which has just completed 
validation.  The request is from %s %s <%s>.  
""" % (irequest.user.first_name, irequest.user.last_name, 
       irequest.user.email),
              settings.DEFAULT_FROM_EMAIL,
              settings.ADMIN_EMAILS, fail_silently=False)


def process_gtfs_done(conn, body):
    build_id = body['id']
    key_map = body['key_map']

    print key_map

    build = GraphBuild.objects.get(id=build_id)

    # create gtfs objects

    for agencyId in key_map:
        try:
            feed = ManagedGtfsFeed.objects.get(s3_key = key_map[agencyId])
        except ManagedGtfsFeed.DoesNotExist:
            print "feed does not exist"
            feed = ManagedGtfsFeed(s3_key=key_map[agencyId], default_agency_id=agencyId)
            feed.save()
        
        mapping = GtfsBuildMapping(gtfs_feed=feed, graph_build=build)
        mapping.save()
        print "saved mapping"

    
    # build feeds

    feeds = [ ]
    for mapping in build.gtfsbuildmapping_set.all():
        gtfsfeed = mapping.gtfs_feed
        feeds.append({ 'key' : gtfsfeed.s3_key, 'defaultAgencyId' : gtfsfeed.default_agency_id }) 

    print "built feeds: %s" % feeds
    # publish build_graph_managed message

    publisher = conn.Producer(routing_key="build_managed", exchange=exchange)
    publisher.publish({'id' : build_id, 'osm_key' : build.deployment.last_osm_key, 'feeds' : feeds})

    print "published b_m message"

def graph_done(conn, body):

    request_id = body['request_id']
    success = body['success']

    irequest = InstanceRequest.objects.get(id=request_id)

    if success:
        graph_key = body['key']

        irequest.state = "graph_built"
        irequest.graph_key = graph_key
        irequest.graph_url = "http://deployer.opentripplanner.org/download_graph?key=%s" % base64.b64encode(graph_key[8:])
        irequest.otp_version = body['otp_version']
        irequest.save()

        send_mail('OTP Deployer Graph Building Complete',
            """Instance request %s has completed graph-building and is ready to be deployed.
            
Download URL: %s""" % (request_id, irequest.graph_url),
            settings.DEFAULT_FROM_EMAIL,
            settings.ADMIN_EMAILS, fail_silently=False)


        #publish a deploy_instance message
        #publisher = conn.Producer(routing_key="deploy_instance", exchange=exchange)
        #publisher.publish({'request_id' : request_id, 'key' : graph_key})

        #start a new deployment instance
        #ec2_conn = connect_ec2(settings.AWS_ACCESS_KEY_ID, settings.AWS_SECRET_KEY) 

        #image = ec2_conn.get_image(settings.DEPLOYMENT_AMI_ID) 

        #print 'Launching EC2 Instance'
        #reservation = image.run(subnet_id=settings.VPC_SUBNET_ID, placement='us-east-1b', key_name='otp-dev', instance_type='m1.large')

        #for instance in reservation.instances:
        #    instance.add_tag("Name", "deploy-req-%s" % request_id)

    else:

        irequest.state = "graph_failed"
        irequest.save()

        send_mail('OTP graph builder failed', 
            """An OTP instance request failed during the graph-building stage.
              
Request ID: %s
""" % (request_id),
            settings.DEFAULT_FROM_EMAIL,
            settings.ADMIN_EMAILS, fail_silently=False)


def osm_extract_done(conn, body):
    
    build = GraphBuild.objects.get(id = body['id'])
    build.osm_key = body['osm_key']
    build.save()
 
    dep = build.deployment
    dep.last_osm_key = body['osm_key']
    dep.save()


def managed_graph_done(conn, body):

    build = GraphBuild.objects.get(id = body['id'])
    build.success = body['success']
    build.otp_version = body['otp_version']
    build.completion_date = datetime.datetime.now()
    build.output_key = body['output_key']
    if body['success']:
        build.graph_key = body['graph_key']

        connection = connect_s3(settings.AWS_ACCESS_KEY_ID, settings.AWS_SECRET_KEY)
        bucket = connection.get_bucket(settings.GRAPH_S3_BUCKET)
        key = bucket.lookup(build.graph_key)
        build.graph_size = int(key.size)
    
    build.save()

    if body['success']:
        # deploy to servers
        pass

def rebuild_graph_done(conn, body):

    request_id = body['request_id']
    success = body['success']

    irequest = InstanceRequest.objects.get(id=request_id)

    if success:
        graph_key = body['key']

        irequest.state = "graph_built"
        irequest.graph_key = graph_key
        irequest.data_key = body['data_key']
        irequest.graph_url = "http://deployer.opentripplanner.org/download_graph?key=%s" % base64.b64encode(graph_key[8:])
        irequest.otp_version = body['otp_version']
        irequest.save()

        send_mail('OTP Deployer Graph Rebuilding Complete',
            """Instance request %s has completed graph rebuilding.
            
Download URL: %s""" % (request_id, irequest.graph_url),
            settings.DEFAULT_FROM_EMAIL,
            settings.ADMIN_EMAILS, fail_silently=False)

    else:
        irequest.state = "graph_failed"
        irequest.save()

        send_mail('OTP graph rebuilding failed', 
            """An OTP instance request failed during the graph rebuilding stage.
              
Request ID: %s
""" % (request_id),
            settings.DEFAULT_FROM_EMAIL,
            settings.ADMIN_EMAILS, fail_silently=False)
	

def deployment_ready(conn, body): 

    if not 'request_id' in body or not 'hostname' in body:
        print 'deployment_ready message missing required parameters'
        return
   
    request_id = body['request_id']
    hostname = body['hostname']
    password = body['password']

    irequest = InstanceRequest.objects.get(id=request_id)
    irequest.state = "deploy_inst"
    irequest.deployment_hostname = hostname
    irequest.admin_password = password
    irequest.save()
    
    # tell proxy server to create mapping
    publisher = conn.Producer(routing_key="setup_proxy", exchange=exchange)
    publisher.publish({'request_id' : request_id, 'hostname' : hostname})

            
def proxy_done(conn, body):

    request_id = body['request_id']
    public_url = body['public_url']

    irequest = InstanceRequest.objects.get(id=request_id)
    irequest.state = "deploy_proxy"
    irequest.public_url = public_url
    irequest.save()

    send_mail('OTP Instance Deployed',
        """An OTP instance for request ID %s was deployed at %s

The graph can be downloaded directly at:
%s

The authenticated API can be accessed via: admin / %s        
""" % (request_id, public_url, irequest.graph_url, irequest.admin_password),
        settings.DEFAULT_FROM_EMAIL,
        settings.ADMIN_EMAILS, fail_silently=False)


def multideployer_ready(conn, body):

    try: 
        dephost = DeploymentHost.objects.get(id=body['request_id'])

        dephost.instance_id = body['instance_id']
        dephost.host_ip = body['host_ip']
        dephost.otp_version = body['otp_version']
        dephost.auth_password = body['auth_password']

        dephost.save()

        # init proxy mapping (for admin access to tomcat)
        publisher = conn.Producer(routing_key="init_proxy_multi", exchange=exchange)
        publisher.publish({'host_id' : dephost.id, 'host_ip' : dephost.host_ip})
    except:
        print "multideployer error"
        traceback.print_exc()


def multideployment_done(conn, body):

    if not 'build_id' in body:
        print 'multideployment_done message missing required parameters'
        return

    build_id = body['build_id']
    build = GraphBuild.objects.get(id=build_id)
    host = DeploymentHost.objects.get(instance_id=body['instance_id'])
    
    mapping = BuildHostMapping(graph_build=build, deployment_host=host)
    mapping.save()
    print "created build-host mapping"

    #irequest = InstanceRequest.objects.get(id=request_id)
    #dephost = irequest.deployment_host
    #irequest.state = "deploy_inst"
    #irequest.admin_password = dephost.auth_password
    #irequest.save()

    # tell proxy server to create mapping
    #publisher = conn.Producer(routing_key="register_proxy_multi", exchange=exchange)
    #publisher.publish({'request_id' : request_id, 'host_ip' : dephost.host_ip})

