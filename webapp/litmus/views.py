import json
import time
from collections import defaultdict
from couchdbkit import Server, ResourceNotFound

from django.conf import settings as DjangoSettings
from django.shortcuts import render_to_response
from django.http import HttpResponse
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import csrf_exempt
from django.core.exceptions import ObjectDoesNotExist
from django.core import serializers

from models import Settings, TestResults, Value


def dashboard(request):
    """Main litmus dashboard"""
    return render_to_response('litmus.jade')


def gen_tag(build):
    """Generate tag, e.g: 2.0.0, 2.0.1 in current impl.
    """
    if build and len(build) > 4:
        return build[:5]
    return "unknown"


def update_or_create(testcase, env, build, metric, value=None, comment=None,
                     color=None):
    """Update testresults/settings if exist, otherwise create new ones.

    :return created     True if created new results, otherwise False
    """

    settings = Settings.objects.get_or_create(testcase=testcase,
                                              metric=metric)[0]

    testresults, created = TestResults.objects.get_or_create(build=build,
                                                             testcase=testcase,
                                                             env=env, metric=metric,
                                                             tag=gen_tag(build),
                                                             settings=settings)
    testresults.timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())
    if value:
        v = Value(value=value)
        testresults.value_set.add(v)
    if comment:
        testresults.comment = comment
    if color:
        testresults.color = color
    testresults.save()

    return created


@csrf_exempt
@require_POST
def post(request):
    """REST API for posting litmus results.

    build -- build version (e.g., '2.0.0-1723-rel-enterprise')
    testcase -- testcase (e.g: 'lucky6')
    env -- test enviornment (e.g, 'terra')
    metric -- metric name (e.g., 'Rebalance time, sec')
    value -- metric value (e.g., 610)

    It supports multivalued query parameters.

    Sample request:
        curl -d "build=2.0.1-118-rel-enterprise\
                 &testcase=lucky6&env=terra\
                 &metric=Get Delay, ms&value=222\
                 &metric=Throughput&value=8793" \
            -X POST http://localhost:8000/litmus/post/
    """
    try:
        build = request.POST['build'].strip()
        testcase = request.POST['testcase'].strip()
        env = request.POST['env'].strip()
        metrics = request.POST.getlist('metric')
        values = request.POST.getlist('value')
    except KeyError, e:
        return HttpResponse(e, status=400)

    for metric, value in zip(metrics, values):
        created = update_or_create(testcase, env, build, metric, value)
    return HttpResponse(content='Created' if created else 'Updated')


@require_GET
def get(request):
    """REST API for getting litmus results.

    Sample request to get both production and experimental test results:
        curl http://localhost:8000/litmus/get?type=all

    Sample request to get experimental test results:
        curl http://localhost:8000/litmus/get?type=exp

    Sample request to get production-wised test results:
        curl http://localhost:8000/litmus/get

    Sample request to get production-wised kv test results:
        curl http://localhost:8000/litmus/get?type=kv

    Sample request to get production-wised view test results:
        curl http://localhost:8000/litmus/get?type=view

    Sample request to get production-wised xdcr test results:
        curl http://localhost:8000/litmus/get?type=xdcr

    Sample request to get specific results:
        curl -G http://localhost:8000/litmus/get \
            -d "testcase=lucky6&metric=Latency,%20ms&tag=2.0.0"

    JSON response:
        [["Testcase", "Env", "Metric", "Timestamp",
         "2.0.0-1723-rel-enterprise", "2.0.0-1724-rel-enterprise"], \
         ["lucky6", "terra", "Query throughput", "2012-10-16 11:10:30",
          "1024", "610"], \
         ["mixed-2suv", "vesta", "Latency, ms", "2012-10-16 11:16:31",
         "777", ""]]
    """
    if "type" not in request.GET:
        objs = TestResults.objects.filter(testcase__in=DjangoSettings.LITMUS_TESTS)
    elif request.GET["type"] == "all":
        objs = TestResults.objects.all()
    elif request.GET["type"] == "exp":
        objs = TestResults.objects.exclude(testcase__in=DjangoSettings.PRODUCTION_TESTS)
    elif request.GET["type"] ==  "kv":
        objs = TestResults.objects.filter(testcase__in=DjangoSettings.LITMUS_KV_TESTS)
    elif request.GET["type"] ==  "view":
        objs = TestResults.objects.filter(testcase__in=DjangoSettings.LITMUS_VIEW_TESTS)
    elif request.GET["type"] == "xdcr":
        objs = TestResults.objects.filter(testcase__in=DjangoSettings.LITMUS_XDCR_TESTS)
    else:
        objs = TestResults.objects.filter(testcase__in=DjangoSettings.PRODUCTION_TESTS)
    if request.GET:
        criteria = dict((key, request.GET[key]) for key in request.GET.iterkeys() if key != "type")
        objs = objs.filter(**criteria)

    builds = list(objs.values('build').order_by('build').reverse().distinct())
    for baseline in DjangoSettings.LITMUS_BASELINE:
        if {'build': baseline} in builds:
            builds.remove({'build': baseline})
            builds.insert(0, {'build': baseline})

    agg_stats = defaultdict(dict)
    for obj in objs:
        key = "%s-%s-%s" % (obj.testcase, obj.env, obj.metric)
        agg_stats[key]['testcase'] = obj.testcase
        agg_stats[key]['env'] = obj.env
        agg_stats[key]['metric'] = obj.metric
        agg_stats[key]['timestamp'] = obj.timestamp
        values = obj.value_set.all()
        if DjangoSettings.LITMUS_AVG_RESULTS:
            agg_stats[key][obj.build] = reduce(lambda x, y: x.value + y.value, values) / len(values)
        else:
            agg_stats[key][obj.build] = "-&-".join([" / ".join(map(lambda v: str(v.value), values)),
                                                    obj.color, obj.comment])

    response = [['Testcase', 'Env', 'Metric', 'Timestamp']
                + [row['build'] for row in builds], ]
    for key, val in agg_stats.iteritems():
        row = [val['testcase'], val['env'], val['metric'], val['timestamp'], ]
        for build in response[0][4:]:
            try:
                row.append(val[build])
            except KeyError:
                row.append('')
        response.append(row)

    return HttpResponse(json.dumps(response), mimetype='application/json')


@csrf_exempt
@require_POST
def post_comment(request):
    """REST API to post comment for litmus results.

    build -- build version (e.g., '2.0.0-1723-rel-enterprise')
    testcase -- testcase (e.g: 'lucky6')
    env -- test enviornment (e.g, 'terra')
    metric -- metric name (e.g., 'Latency, ms')
    comment -- comment string (e.g, 'Regression for RC1')

    Sample request:
        curl -d "testcase=lucky6\
                 &env=terra\
                 &build=2.0.0-1723-rel-enterprise\
                 &metric=Latency, ms\
                 &comment=Regression for RC1" \
            -X POST http://localhost:8000/litmus/post/comment/
    """
    try:
        testcase = request.POST['testcase'].strip()
        env = request.POST['env'].strip()
        build = request.POST['build'].strip()
        metric = request.POST['metric'].strip()
        comment = request.POST['comment'].strip()
    except KeyError, e:
        return HttpResponse(e, status=400)

    update_or_create(testcase, env, build, metric, comment=comment)

    return HttpResponse(content=comment)


@require_GET
def get_comment(request):
    """REST API to get comment for litmus results.

    build -- build version (e.g., '2.0.0-1723-rel-enterprise')
    testcase -- testcase (e.g: 'lucky6')
    env -- test enviornment (e.g, 'terra')
    metric -- metric name (e.g., 'Rebalance time, sec')

    Sample request:
        curl -G http://localhost:8000/litmus/get/comment \
            -d "testcase=lucky6&env=terra&build=2.0.0-1723-rel-enterprise&metric=Latency,%20ms"
    """
    try:
        testcase = request.GET['testcase'].strip()
        env = request.GET['env'].strip()
        build = request.GET['build'].strip()
        metric = request.GET['metric'].strip()
    except KeyError, e:
        return HttpResponse(e, status=400)

    objs = TestResults.objects.filter(testcase=testcase, env=env,
                                      build=build, metric=metric).distinct()

    if not objs:
        return HttpResponse("empty result set", status=404)

    return HttpResponse(content=objs[0].comment)


@require_GET
def get_settings(request):
    """REST API to get settings for litmus results.

    testcase -- testcase (e.g: 'lucky6')
    metric -- metric name (e.g., 'Latency, ms')

    Sample request to get all settings:
        curl http://localhost:8000/litmus/get/settings?all
        curl http://localhost:8000/litmus/get/settings

    Sample request to get a specific setting:
        curl -G http://localhost:8000/litmus/get/settings \
            -d "testcase=lucky6&metric=Latency,%20ms"
    """
    if not request.GET or 'all' in request.GET:
        response = map(lambda d: d["fields"],
                       serializers.serialize("python", Settings.objects.all()))
        return HttpResponse(content=json.dumps(response))

    try:
        testcase = request.GET['testcase'].strip()
        metric = request.GET['metric'].strip()
    except KeyError, e:
        return HttpResponse(e, status=400)

    try:
        obj = Settings.objects.get(testcase=testcase, metric=metric)
    except ObjectDoesNotExist:
        return HttpResponse("empty result set", status=404)

    response = map(lambda d: d["fields"],
                   serializers.serialize("python", [obj, ]))

    return HttpResponse(content=json.dumps(response))


@require_GET
def get_tags(request):
    """ REST API to get dinstinctive tags

    Sample request to get all settings:
        curl http://localhost:8000/litmus/get/tags
    """
    vals = TestResults.objects.values('tag').order_by('tag').distinct()
    tags = map(lambda val: val['tag'], vals)
    return HttpResponse(content=json.dumps(tags))


@csrf_exempt
@require_POST
def post_color(request):
    """REST API to change background color for litmus results.

    build -- build version (e.g., '2.0.0-1723-rel-enterprise')
    testcase -- testcase (e.g: 'lucky6')
    env -- test enviornment (e.g, 'terra')
    metric -- metric name (e.g., 'Latency, ms')
    color -- color string (e.g,  'white', '#90ee90')

    Sample request:
        curl -d "testcase=lucky6\
                 &env=terra\
                 &build=2.0.0-1723-rel-enterprise\
                 &metric=Latency, ms\
                 &color=red" \
            -X POST http://localhost:8000/litmus/post/color/
    """
    try:
        testcase = request.POST['testcase'].strip()
        env = request.POST['env'].strip()
        build = request.POST['build'].strip()
        metric = request.POST['metric'].strip()
        color = request.POST['color'].strip()
    except KeyError, e:
        return HttpResponse(e, status=400)

    update_or_create(testcase, env, build, metric, color=color)

    return HttpResponse(content=color)


@require_GET
def get_color(request):
    """REST API to get color for litmus results.

    build -- build version (e.g., '2.0.0-1723-rel-enterprise')
    testcase -- testcase (e.g: 'lucky6')
    env -- test enviornment (e.g, 'terra')
    metric -- metric name (e.g., 'Rebalance time, sec')

    Sample request:
        curl -G http://localhost:8000/litmus/get/color \
            -d "testcase=lucky6&env=terra&build=2.0.0-1723-rel-enterprise&metric=Latency,%20ms"
    """
    try:
        testcase = request.GET['testcase'].strip()
        env = request.GET['env'].strip()
        build = request.GET['build'].strip()
        metric = request.GET['metric'].strip()
    except KeyError, e:
        return HttpResponse(e, status=400)

    objs = TestResults.objects.filter(testcase=testcase, env=env,
                                      build=build, metric=metric).distinct()

    if not objs:
        return HttpResponse("empty result set", status=404)

    return HttpResponse(content=objs[0].color)

def _graph_key(testcase, env, base, target, phase="loop"):
    return ", ".join([ "%s.%s" % (testcase, phase) , env, base, target])

@require_GET
def get_graphs(request):
    """REST API to get pdf graph links for the test.

    testcase -- testcase (e.g: 'lucky6')
    env -- test enviornment (e.g, 'terra')
    base -- baseline build version (e.g., '2.0.0-1723-rel-enterprise')
    target -- target build version (e.g., '2.0.0-1723-rel-enterprise')

    Sample request:
        curl -G http://localhost:8000/litmus/get/graphs \
            -d "testcase=lucky6&env=terra&base=2.0.0-1723-rel-enterprise&target=2.0.0-1723-rel-enterprise"
    """
    try:
        testcase = request.GET['testcase'].strip()
        env = request.GET['env'].strip()
        base = request.GET['base'].strip()
        target = request.GET['target'].strip()
    except KeyError, e:
        return HttpResponse(e, status=400)

    server = Server(DjangoSettings.LITMUS_GRAPH_URL)
    db_name = testcase.split('-')[0]
    db = server.get_or_create_db(db_name)

    response = []
    try:
        for row in db.view(DjangoSettings.LITMUS_GRAPH_VIEW_PATH,
                           key=_graph_key(testcase, env, base, target)):
            time = row['value']['_time']
            link = "/".join([DjangoSettings.LITMUS_GRAPH_URL,
                             db_name, row['id'], row['value']['_attachments'].keys()[0]])
            response.append([time, link])
    except ResourceNotFound:
        return HttpResponse(json.dumps(''), mimetype='application/json')
    except Exception, e:
        return HttpResponse(e, status=400)

    return HttpResponse(json.dumps(response), mimetype='application/json')
