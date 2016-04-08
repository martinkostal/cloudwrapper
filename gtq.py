"""Google Task Pull Queues."""

from Queue import Empty

from time import sleep
from gcloud_taskqueue import Taskqueue, Client
from gcloud.exceptions import GCloudError
from googleapiclient.discovery import build
from oauth2client.client import GoogleCredentials

from .base import BaseQueue

import json
import errno
import time


class GtqConnection(object):

    def __init__(self):
        self.client = Client()

    def queue(self, name):
        return Queue(Taskqueue(client=self.client, id=name), self.client)


class Queue(BaseQueue):
    """
    Google Task Pull Queues

    Note that items gotten from the queue MUST be acknowledged by
    calling task_done(). Otherwise they will appear back in the
    queue, after a period of time called 'visibility time'. This
    parameter, and others, are configured outside this module.
    """

    def __init__(self, handle, client):
        self.handle = handle
        self.client = client
        self.message = None
        self.available_timestamp = None
        self._reconnect()


    def _reconnect(self):
        credentials = GoogleCredentials.get_application_default()
        self.handle_api = build('taskqueue', 'v1beta2', credentials=credentials)


    def qsize(self):
        """Implemented via REST API
        GET https://www.googleapis.com/taskqueue/v1beta2/projects/project/taskqueues/taskqueue?getStats=true
        Response: see https://cloud.google.com/appengine/docs/python/taskqueue/rest/taskqueues#resource
        """
        try :
            taskqueue = self.handle_api.taskqueues().get(project=self.handle.project, taskqueue=self.handle.id, getStats=True).execute()
        except:
            return 0
        return int(taskqueue['stats']['totalTasks'])

    def put(self, item, block=True, timeout=None, delay=None):
        """Put item into the queue.

        Note that GTQ doesn't implement non-blocking or timeouts for writes,
        so both 'block' and 'timeout' must have their default values only.
        """
        if not (block and timeout is None):
            raise Exception('block and timeout must have default values')
        for _ in range(6):
            try:
                self.handle.insert_task(description=json.dumps(item), client=self.client)
                break
            except IOError as e:
                if e.errno == errno.EPIPE:
                    self.client = Client()
                sleep(10)
            except GCloudError:
                sleep(30)

    def _get_message(self, lease_time):
        """Get one message with lease_time
        """
        for _ in range(6):
            try:
                for task in self.handle.lease(lease_time=lease_time, num_tasks=1, client=self.client):
                    return task
                return None
            except IOError as e:
                if e.errno == errno.EPIPE:
                    self.client = Client()
                sleep(10)
            except GCloudError:
                sleep(30)

    def get(self, block=True, timeout=None, lease_time=3600):
        """Get item from the queue.

        Default lease_time is 1 hour.
        """

        self.message = self._get_message(lease_time)
        if block:
            while self.message is None:
                # Sleep at least 20 seconds before next message receive
                sleep(timeout if timeout is not None else 20)
                self.message = self._get_message(lease_time)

        if self.message is None:
            raise Empty

        return json.loads(self.message.description)

    def task_done(self):
        """Acknowledge that a formerly enqueued task is complete.

        Note that this method MUST be called for each item.
        See the class docstring for details.
        """
        if self.message is None:
            raise Exception('no message to acknowledge')
        for _ in range(6):
            try:
                self.message.delete(client=self.client)
                self.message = None
                break
            except IOError as e:
                if e.errno == errno.EPIPE:
                    self.client = Client()
                sleep(10)
            except GCloudError:
                sleep(30)


    def has_available(self):
        """Is any message available for lease.

        If there is no message, this state is cached internally for 5 minutes.
        10 minutes is time used for Google Autoscaler.
        """
        now = time.time()
        # We have cached False response
        if self.available_timestamp is not None and self.available_timestamp < now:
            print(['GTQ: has_available check, cached FALSE: now, cache timestamp', now, self.available_timestamp])
            return False

        # Get oldestTask from queue stats
        exc = None
        for _ in range(6):
            try:
                taskqueue = self.handle_api.taskqueues().get(project=self.handle.project, taskqueue=self.handle.id, getStats=True).execute()
                print(['GTQ: Received stats for Queue '+self.handle.id, taskqueue])
                break
            except IOError as e:
                exc = e
                print(['GTQ: Catched IOError exception', e, e.errno])
                if e.errno == errno.EPIPE:
                    self._reconnect()
                sleep(10)
            except Exception as e:
                exc = e
                print(['GTQ: Catched general exception', e, e.errno])
                sleep(10)
        else:
            print(['GTQ: Cannot get stats with 6 attempts, saved Exception: ', exc])
            if exc is not None:
                raise exc
            return False
        # There is at least one availabe task
        if float(taskqueue['stats']['oldestTask']) < now:
            print(['GTQ: OldestTask timeout is "ago", there is something available: ', 
                float(taskqueue['stats']['oldestTask']), float(taskqueue['stats']['oldestTask']) < now])
            return True
        # No available task, cache this response for 5 minutes
        self.available_timestamp = now + 300 # 5 minutes
        print(['GTQ: Caching FALSE response for 5 minutes: ', self.available_timestamp])
        return False

