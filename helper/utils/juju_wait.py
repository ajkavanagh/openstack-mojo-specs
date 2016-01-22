#!/usr/bin/python3

# This file is part of juju-wait, a juju plugin to wait for environment
# steady state.
#
# Copyright 2015 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3, as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranties of
# MERCHANTABILITY, SATISFACTORY QUALITY, or FITNESS FOR A PARTICULAR
# PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
from datetime import datetime, timedelta
from distutils.version import LooseVersion
import json
import logging
import os
import subprocess
import sys
from textwrap import dedent
import time


class DescriptionAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        parser.exit(0, parser.description.splitlines()[0] + '\n')


class EnvironmentAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        os.environ['JUJU_ENV'] = values[0]


def parse_ts(ts):
    '''Parse the Juju provided timestamp, which must be UTC.'''
    return datetime.strptime(ts, '%d %b %Y %H:%M:%SZ')


class JujuWaitException(Exception):
    '''A fatal exception'''
    pass


def run_or_die(cmd, env=None):
    try:
        # It is important we don't mix stdout and stderr, as stderr
        # will often contain SSH noise we need to ignore due to Juju's
        # lack of SSH host key handling.
        p = subprocess.Popen(cmd, universal_newlines=True, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        (out, err) = p.communicate()
    except OSError as x:
        logging.error("{} failed: {}".format(' '.join(cmd), x.errno))
        raise JujuWaitException(x.errno or 41)
    except Exception as x:
        logging.error("{} failed: {}".format(' '.join(cmd), x))
        raise JujuWaitException(42)
    if p.returncode != 0:
        logging.error(err)
        logging.error("{} failed: {}".format(' '.join(cmd), p.returncode))
        raise JujuWaitException(p.returncode or 43)
    return out


def juju_run(unit, cmd, timeout=None):
    if timeout is None:
        timeout = 6 * 60 * 60
    return run_or_die(['juju', 'run', '--timeout={}s'.format(timeout),
                       '--unit', unit, cmd])


def get_status():
    # Older juju versions don't support --utc, so force UTC timestamps
    # using the environment variable.
    env = os.environ.copy()
    env['TZ'] = 'UTC'
    env['JUJU_CLI_VERSION'] = '1'
    json_status = run_or_die(['juju', 'status', '--format=json'], env=env)
    if json_status is None:
        return None
    return json.loads(json_status)


def get_log_tail(unit, timeout=None):
    log = 'unit-{}.log'.format(unit.replace('/', '-'))
    cmd = 'sudo tail -1 /var/log/juju/{}'.format(log)
    return juju_run(unit, cmd, timeout=timeout)


def get_is_leader(unit, timeout=None):
    raw = juju_run(unit, 'is-leader --format=json', timeout=timeout)
    return json.loads(raw)


# Juju 1.24+ provides us with the timestamp the status last changed.
# If all units are idle more than this many seconds, the system is
# quiescent. This may be unnecessary, but protects against races
# where all units report they are currently idle but there are hooks
# still due to be run.
IDLE_CONFIRMATION = timedelta(seconds=15)


def wait_cmd(args=sys.argv[1:]):
    description = dedent("""\
        Wait for environment steady state.

        The environment is considered in a steady state once all hooks
        have completed running and there are no hooks queued to run,
        on all units.

        If you need a timeout, use the timeout(1) tool.
        """)
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('-e', '--environment', metavar="ENV", type=str,
                        action=EnvironmentAction, nargs=1)
    parser.add_argument('--description', action=DescriptionAction, nargs=0)
    parser.add_argument('-q', '--quiet', dest='quiet',
                        action='store_true', default=False)
    parser.add_argument('-v', '--verbose', dest='verbose',
                        action='store_true', default=False)
    args = parser.parse_args(args)

    # Parser did not exit, so continue.
    logging.basicConfig()
    log = logging.getLogger()
    if args.quiet:
        log.setLevel(logging.WARN)
    elif args.verbose:
        log.setLevel(logging.DEBUG)
    else:
        log.setLevel(logging.INFO)
    try:
        wait(log)
        return 0
    except JujuWaitException as x:
        return x.args[0]


def reset_logging():
    """If we are running Juju 1.23 or earlier, we require default logging.

    Reset the environment log settings to match default juju stable.
    """
    run_or_die(['juju', 'set-environment',
                'logging-config=juju=WARNING;unit=INFO'])


def wait(log=None):
    if log is None:
        log = logging.getLogger()

    # pre-juju 1.24, we can only detect idleless by looking for changes
    # in the logs.
    prev_logs = {}

    ready_since = None

    logging_reset = False

    while True:
        status = get_status()
        ready = True

        # If there is a dying service, environment is not quiescent.
        for sname, service in sorted(status.get('services', {}).items()):
            if service.get('life') in ('dying', 'dead'):
                logging.debug('{} is dying'.format(sname))
                ready = False

        all_units = set()  # All units, including subordinates.

        # 'ready' units are up, and might be idle. They need to have their
        # logs sniffed because they are running Juju 1.23 or earlier.
        ready_units = {}

        # Flattened agent status for all units and subordinates that
        # provide it. Note that 'agent status' is only available in
        # Juju 1.24 and later. This is easily confused with 'agent state'
        # which is available in earlier versions of Juju.
        agent_status = {}
        agent_version = {}
        for sname, service in status.get('services', {}).items():
            for uname, unit in service.get('units', {}).items():
                all_units.add(uname)
                agent_version[uname] = unit.get('agent-version')
                if 'agent-status' in unit:
                    agent_status[uname] = unit['agent-status']
                else:
                    ready_units[uname] = unit  # Schedule for sniffing.
                for subname, sub in unit.get('subordinates', {}).items():
                    agent_version[subname] = sub.get('agent-version')
                    if 'agent-status' in sub:
                        agent_status[subname] = sub['agent-status']
                    else:
                        ready_units[subname] = sub  # Schedule for sniffing.

        for uname, astatus in sorted(agent_status.items()):
            current = astatus['current']
            since = parse_ts(astatus['since'])
            logging.debug('{} is {} since {}Z'.format(uname, current, since))
            if current != 'idle':
                ready = False

        # Log storage to compare with prev_logs.
        logs = {}

        # Sniff logs of units that don't provide agent-status, if necessary.
        for uname, unit in sorted(ready_units.items()):
            dying = unit.get('life') in ('dying', 'dead')
            agent_state = unit.get('agent-state')
            agent_state_info = unit.get('agent-state-info')
            if dying:
                logging.debug('{} is dying'.format(uname))
                ready = False
            elif agent_state == 'error':
                logging.error('{} failed: {}'.format(uname, agent_state_info))
                ready = False
                raise JujuWaitException(1)
            elif agent_state != 'started':
                logging.debug('{} is {}'.format(uname, agent_state))
                ready = False
            elif ready:
                if not logging_reset:
                    reset_logging()
                    logging_reset = True
                logs[uname] = get_log_tail(uname)
                if logs[uname] == prev_logs.get(uname):
                    logging.debug('{} is idle - no hook activity'
                                  ''.format(uname))
                else:
                    logging.debug('{} is active: {}'
                                  ''.format(uname, logs[uname].strip()))
                    ready = False

        # Ensure every service has a leader. If there is no leader, then
        # one will be appointed soon and hooks should kick off.
        if ready:
            services = set()
            services_with_leader = set()
            for uname, version in agent_version.items():
                sname = uname.split('/', 1)[0]
                services.add(sname)
                if (sname not in services_with_leader and version
                    and (LooseVersion(version) >= LooseVersion('1.23')
                         or get_is_leader(uname) is True)):
                    services_with_leader.add(sname)
                    logging.debug('{} is lead by {}'.format(sname, uname))
            for sname in services:
                if sname not in services_with_leader:
                    logging.info('{} does not have a leader'.format(sname))
                    ready = False

        if ready:
            # We are never ready until this check has been running until
            # IDLE_CONFIRMATION time has passed. This ensures that if we
            # run 'juju wait' immediately after an operation such as
            # 'juju upgrade-charm', then the scheduled operation has had
            # a chance to fire any hooks it is going to.
            if ready_since is None:
                ready_since = datetime.utcnow()
            elif ready_since + IDLE_CONFIRMATION < datetime.utcnow():
                logging.info('All units idle since {}Z ({})'
                             ''.format(ready_since,
                                       ', '.join(sorted(all_units))))
                return
        else:
            ready_since = None

        prev_logs = logs
        time.sleep(4)


if __name__ == '__main__':
    # I use these to launch the entry points from the source tree.
    # Most installations will be using the setuptools generated
    # launchers.
    script = os.path.basename(sys.argv[0])
    sys.exit(wait_cmd())