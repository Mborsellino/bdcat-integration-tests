#!/usr/bin/env python
# coding: utf-8
import requests
import os
import sys
import argparse
import json

from threading import Thread

pkg_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))  # noqa
sys.path.insert(0, pkg_root)  # noqa

from scripts.run_integration_tests import wait_for_final_status, DEFAULT_BRANCH, DEFAULT_HOST, DEFAULT_PROJECT_NUM

SLACK_WEBHOOK = os.environ['SLACK_WEBHOOK']

# https://docs.gitlab.com/ee/ci/variables/predefined_variables.html
GITLAB_USER_NAME = os.environ['GITLAB_USER_NAME']
CI_JOB_URL = os.environ['CI_JOB_URL']
CI_PIPELINE_URL = os.environ['CI_PIPELINE_URL']

def post_notification(host, project):
    slack_notification_url = 'https://hooks.slack.com/services/' + SLACK_WEBHOOK
    headers = {
        'Content-type': 'application/json'
    }
    status = wait_for_final_status(pipeline=CI_PIPELINE_URL.split('/')[-1],
                                   host=host,
                                   project=project,
                                   quiet=True)
    data = {
        'text': f'{GITLAB_USER_NAME} triggered: <{CI_JOB_URL}>\n'
                f'Status is: {status}'
    }
    response = requests.post(slack_notification_url, data=json.dumps(data), headers=headers)
    response.raise_for_status()


def main(argv=sys.argv[1:]):
    parser = argparse.ArgumentParser(description='Post integration testing status to slack.')
    parser.add_argument("--project", type=int, default=DEFAULT_PROJECT_NUM)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--host", default=DEFAULT_HOST)
    args = parser.parse_args(argv)

    t = Thread(target=post_notification, kwargs=dict(host=args.host, project=args.project))
    t.daemon = True
    t.start()


if __name__ == '__main__':
    main()
