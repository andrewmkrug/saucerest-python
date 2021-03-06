#! /usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2009 Sauce Labs Inc
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# 'Software'), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED 'AS IS', WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import time
import httplib2
import urllib
import socket
import logging

import simplejson  # http://cheeseshop.python.org/pypi/simplejson

logger = logging.getLogger(__name__)


class SauceRestError(Exception):
    pass


def _loads(json):
    try:
        return simplejson.loads(json)
    except:
        raise SauceRestError("Invalid JSON response: %s", json)


class SauceClient:
    """Basic wrapper class for operations with Sauce"""

    def __init__(self, name=None, access_key=None,
                 base_url="https://saucelabs.com",
                 timeout=30):
        if base_url.endswith('/'):
            base_url = base_url[:-1]
        self.base_url = base_url
        self.account_name = name
        self.unhealthy_tunnels = set()
        self.http = httplib2.Http(timeout=timeout)
        self.http.add_credentials(name, access_key)

        # Used for job/batch waiting
        self.SLEEP_INTERVAL = 5   # in seconds
        self.TIMEOUT = 300  # TIMEOUT/60 = number of minutes before timing out

    def _http_request(self, uri, method, **keywords):
        """Wrap the HTTP request up so we get reasonable error handling."""
        try:
            return self.http.request(uri, method, **keywords)
        except (httplib2.ServerNotFoundError, socket.error), e:
            raise SauceRestError(
                "HTTP request failed for %s: %s" % (self.base_url, e))
        except AttributeError:
            # httplib2 errors suck
            raise SauceRestError("HTTP request failed for %s (httplib2"
                " maybe couldn't create socket/connection)" % self.base_url)

    def get(self, type, doc_id, **kwargs):
        headers = {"Content-Type": "application/json"}
        attachment = ""
        if 'attachment' in kwargs:
            attachment = "/%s" % kwargs.pop('attachment')
        if kwargs:
            parameters = "?%s" % (urllib.urlencode(kwargs))
        else:
            parameters = ""
        url = self.base_url + "/rest/%s/%s/%s%s%s" % (self.account_name,
                                                      type,
                                                      doc_id,
                                                      attachment,
                                                      parameters)
        response, content = self._http_request(url, 'GET', headers=headers)
        if attachment:
            return content
        else:
            return _loads(content)

    def list(self, type):
        headers = {"Content-Type": "application/json"}
        url = self.base_url + "/rest/%s/%s" % (self.account_name, type)
        response, content = self._http_request(url, 'GET', headers=headers)
        return _loads(content)

    def create(self, type, body):
        headers = {"Content-Type": "application/json"}
        url = self.base_url + "/rest/%s/%s" % (self.account_name, type)
        body = simplejson.dumps(body)
        response, content = self._http_request(url,
                                              'POST',
                                              body=body,
                                              headers=headers)
        return _loads(content)

    def attach(self, doc_id, name, body):
        url = self.base_url + "/rest/%s/scripts/%s/%s" % (self.account_name,
                                                          doc_id, name)
        response, content = self._http_request(url, 'PUT', body=body)
        return _loads(content)

    def delete(self, type, doc_id):
        headers = {"Content-Type": "application/json"}
        url = self.base_url + "/rest/%s/%s/%s" % (self.account_name,
                                                  type,
                                                  doc_id)
        response, content = self._http_request(url, 'DELETE', headers=headers)
        return _loads(content)

    #------ Sauce-specific objects ------

    # Scripts

    def create_script(self, body):
        return self.create('scripts', body)

    def get_script(self, script_id):
        return self.get('scripts', doc_id=script_id)

    # Jobs

    def create_job(self, body):
        return self.create('jobs', body)

    def get_job(self, job_id):
        return self.get('jobs', job_id)

    def list_jobs(self):
        return self.list('jobs')

    def wait_for_jobs(self, batch_id):
        t = 0
        while t < self.TIMEOUT:
            jobs = self.get_jobs(batch=batch_id)
            total_comp = len([j for j in jobs if j['Status'] == 'complete'])
            total_err = len([j for j in jobs if j['Status'] == 'error'])

            if total_comp + total_err == len(jobs):
                return

            time.sleep(self.SLEEP_INTERVAL)
            t += self.SLEEP_INTERVAL

        if t >= self.TIMEOUT:
            raise Exception("Timed out waiting for all jobs to finish")

    # Tunnels

    def create_tunnel(self, body):
        return self.create('tunnels', body)

    def get_tunnel(self, tunnel_id):
        return self.get('tunnels', tunnel_id)

    def list_tunnels(self):
        return self.list('tunnels')

    def delete_tunnel(self, tunnel_id):
        return self.delete('tunnels', tunnel_id)

    def delete_tunnels_by_domains(self, domains):
        logger.info(
            "Searching for existing tunnels using requested domains ...")
        for tunnel in self.list_tunnels():
            for domain in (d for d in domains if d in tunnel['DomainNames']):
                logger.warning("Tunnel %s is currenty using requested"
                               " domain %s" % (tunnel['_id'], domain))
                logger.info("Shutting down tunnel %s" % tunnel['_id'])
                self.delete_tunnel(tunnel['_id'])

    # -- Tunnel utilities

    def _is_ssh_host_up(self, host, port=22, timeout=10, connect_tries=3):
        """Return whether we receive the SSH string from the host port."""

        for i in xrange(connect_tries):
            sock = socket.socket()
            sock.settimeout(timeout) # timeout in secs
            try:
                # these block until timeout
                sock.connect((host, port))
                data = sock.recv(4096)
            except socket.timeout:
                logger.warning("Socket timed out trying to connect to SSH host")
            except socket.error, err:
                logger.error("Socket error when trying to connect to SSH host: %s"
                             % err)
            else:
                if data and data.startswith("SSH-"):
                    if i:
                        logger.error("SSH health check succeeded (%s/%s)", i,
                                     connect_tries)
                    return True
                logger.error("Got unexpected data from SSH server: '%s'" % data)
                return False
            if i+1 < connect_tries:
                logger.error("Retrying SSH health check (%s/%s)", i+1,
                             connect_tries)


    def is_tunnel_healthy(self, tunnel_id):
        """Return whether a tunnel connection is considered healthy."""
        if tunnel_id in self.unhealthy_tunnels:
            self.unhealthy_tunnels.remove(tunnel_id)
            return False
        try:
            tunnel = self.get_tunnel(tunnel_id)
        except SauceRestError, e:
            logger.warning("Could not get tunnel info: %s" % e)
            return False
        if tunnel['Status'] != 'running':
            logger.debug(
                "Tunnel has non-running status '%s'" % tunnel['Status'])
            return False
        return self._is_ssh_host_up(tunnel['Host'])

    def prune_unhealthy_tunnels(self, tunnels_of_concern):
        self.unhealthy_tunnels.intersection_update(tunnels_of_concern)
