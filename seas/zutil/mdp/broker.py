import time
import logging
from collections import deque

import zmq

from . import constants as MDP
from . import err
from seas.zutil.heartbeat import HeartbeatManager


log = logging.getLogger(__name__)


class MajorDomoBroker(object):

    def __init__(self, uri, **kwargs):
        self.uri = uri
        self.heartbeat = HeartbeatManager(
            kwargs.pop('heartbeat_interval', 1.0),
            kwargs.pop('heartbeat_liveness', 3))
        self.poll_interval = kwargs.pop('poll_interval', 1.0)
        self.request_timeout = kwargs.pop('request_timeout', 5)
        self.socket = None
        self._control_uri = 'inproc://mdp-broker-{}'.format(id(self))
        self._services = {}
        self._workers = {}

    def serve_forever(self, context=None):
        if context is None:
            context = zmq.Context.instance()
        for x in self.reactor(context):
            pass

    def reactor(self, context=None):
        if context is None:
            context = zmq.Context.instance()
        log.debug('In reactor %s', self._control_uri)
        self.socket = self.make_socket(context, zmq.ROUTER)
        control = self.make_socket(context, zmq.PULL)
        self.socket.bind(self.uri)
        control.bind(self._control_uri)
        self.poller = zmq.Poller()
        self.poller.register(control, zmq.POLLIN)
        self.poller.register(self.socket, zmq.POLLIN)

        while True:
            yield self.poller
            socks = dict(self.poller.poll(self.poll_interval))
            if control in socks:
                msg = control.recv()
                log.debug('control: %s', msg)
                if msg == 'TERMINATE':
                    log.debug('Terminating reactor')
                    control.close()
                    self.socket.close()
                    self.socket = None
                    break
            if self.socket in socks:
                msg = self.socket.recv_multipart()
                log.debug('recv:\n%r', msg)
                self.handle_message(list(reversed(msg)))

        self.send_heartbeats()
        self.reap_workers()

    def make_socket(self, context, socktype):
        socket = context.socket(socktype)
        socket.linger = 0
        return socket

    def handle_message(self, rmsg):
        sender_addr = rmsg.pop()
        assert rmsg.pop() == ''
        magic = rmsg.pop()
        if magic == MDP.C_CLIENT:
            self.handle_client(sender_addr, rmsg)
        elif magic == MDP.W_WORKER:
            self.handle_worker(sender_addr, rmsg)
        else:
            raise err.UnknownMagic(magic)

    def handle_client(self, sender_addr, rmsg):
        service_name = rmsg.pop()
        service = self.require_service(service_name)
        service.queue_request(sender_addr, rmsg, self.request_timeout)
        service.dispatch()

    def handle_worker(self, sender_addr, rmsg):
        command = rmsg.pop()
        self.heartbeat.hear_from(sender_addr)
        if command == MDP.W_READY:
            worker = self.require_worker(sender_addr)
            service = self.require_service(rmsg.pop())
            worker.register(service)
            worker.ready()
            service.dispatch()
        elif command == MDP.W_REPLY:
            worker = self.require_worker(sender_addr)
            client_addr = rmsg.pop()
            assert rmsg.pop() == ''
            payload = list(reversed(rmsg))
            self.send_to_client(client_addr, worker.service.name, *payload)
            worker.ready()
        else:
            raise NotImplementedError()

    def stop(self, context=None):
        if context is None:
            context = zmq.Context.instance()
        if self.socket:
            log.debug('Send TERMINATE to %s', self._control_uri)
            sock = self.make_socket(context, zmq.PUSH)
            sock.connect(self._control_uri)
            sock.send('TERMINATE')
            sock.close()

    def destroy(self):
        if self.socket:
            self.socket.close()

    def send_to_client(self, client_addr, service_name, *parts):
        prefix = [client_addr, '', MDP.C_CLIENT, service_name]
        self.socket.send_multipart(prefix + list(parts))

    def send_to_worker(self, worker_addr, command, *parts):
        prefix = [worker_addr, '', MDP.W_WORKER, command]
        self.heartbeat.send_to(worker_addr)
        self.socket.send_multipart(prefix + list(parts))

    def require_service(self, service_name):
        service = self._services.get(service_name)
        if service is None:
            service = Service(self, service_name)
            self._services[service_name] = service
        return service

    def require_worker(self, worker_addr):
        worker = self._workers.get(worker_addr)
        if worker is None:
            worker = Worker(self, worker_addr)
            self._workers[worker_addr] = worker
        return worker

    def send_heartbeats(self):
        for addr in self.heartbeat.need_beats():
            self.send_to_worker(addr, MDP.HEARTBEAT)

    def reap_workers(self):
        for addr in self.heartbeat.reap():
            worker = self.require_worker(addr)
            worker.delete(False)


class Worker(object):

    def __init__(self, broker, addr):
        self.broker = broker
        self.addr = addr
        self.service = None

    def register(self, service):
        if self.service:
            log.debug('Worker already registered for %s', self.service.name)
            self.delete(True)
        self.service = service

    def ready(self):
        self.service.worker_ready(self)

    def delete(self, disconnect):
        if disconnect:
            self._broker.send_to_worker(self.addr, MDP.W_DISCONNECT)
        if self.service is not None:
            self.service.unregister_worker(self)
        self.brokers.heartbeat.discard_peer(self.addr)

    def handle_client(self, client_addr, rmsg):
        payload = list(reversed(rmsg))
        self.broker.send_to_worker(
            self.addr, MDP.W_REQUEST, client_addr, '', *payload)


class Service(object):

    def __init__(self, broker, name):
        self.broker = broker
        self.name = name
        self.requests = deque()         # (exp, sender, body)
        self._workers_ready = deque()    # workers available
        self.workers = {}

    def worker_ready(self, worker):
        self._workers_ready.appendleft(worker.addr)
        self.workers[worker.addr] = worker

    def unregister_worker(self, worker):
        self.workers.pop(worker.addr, None)

    def queue_request(self, client_addr, rmsg, timeout):
        log.debug('queueing request from %s', client_addr)
        self.requests.appendleft((time.time() + timeout, client_addr, rmsg))

    def dispatch(self):
        while self.requests and self._workers_ready:
            (exp, client_addr, rmsg) = self.requests.pop()
            if exp < time.time():
                continue
            worker = self.pop_ready_worker()
            if worker is None:
                self.requests.append((exp, client_addr, rmsg))
                break
            worker.handle_client(client_addr, rmsg)

    def pop_ready_worker(self):
        while self._workers_ready:
            addr = self._workers_ready.pop()
            worker = self.workers.get(addr)
            if worker is not None:
                return worker
        return None



