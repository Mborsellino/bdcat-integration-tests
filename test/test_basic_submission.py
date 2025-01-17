#!/usr/bin/env python3
import logging
import sys
import unittest
import os
import json
import time
from typing import Dict
from unittest import TextTestRunner, TextTestResult

import requests
import datetime
import warnings
import base64

from terra_notebook_utils import drs


pkg_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))  # noqa
sys.path.insert(0, pkg_root)  # noqa

from test.bq import log_duration, Client
from test.infra.testmode import staging_only
from test.utils import (run_workflow,
                        create_terra_workspace,
                        delete_terra_workspace,
                        pfb_job_status_in_terra,
                        import_pfb,
                        retry,
                        check_terra_health,
                        import_dockstore_wf_into_terra,
                        check_workflow_presence_in_terra_workspace,
                        delete_workflow_presence_in_terra_workspace,
                        check_workflow_status,
                        import_drs_with_direct_gen3_access_token,
                        BILLING_PROJECT,
                        STAGE)

logger = logging.getLogger(__name__)


class TestGen3DataAccess(unittest.TestCase):
    def setUp(self):
        # Stolen shamelessly: https://github.com/DataBiosphere/terra-notebook-utils/pull/59
        # Suppress the annoying google gcloud _CLOUD_SDK_CREDENTIALS_WARNING warnings
        warnings.filterwarnings("ignore", "Your application has authenticated using end user credentials")
        # Suppress unclosed socket warnings
        warnings.simplefilter("ignore", ResourceWarning)

    @classmethod
    def setUpClass(cls):
        gcloud_cred_dir = os.path.expanduser('~/.config/gcloud')
        if not os.path.exists(gcloud_cred_dir):
            os.makedirs(gcloud_cred_dir, exist_ok=True)
        with open(os.path.expanduser('~/.config/gcloud/application_default_credentials.json'), 'w') as f:
            f.write(base64.decodebytes(os.environ['TEST_MULE_CREDS'].encode('utf-8')).decode('utf-8'))
        print(f'Terra [{STAGE}] Health Status:\n\n{json.dumps(check_terra_health(), indent=4)}')

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            delete_workflow_presence_in_terra_workspace()
        except:  # noqa
            pass

    @retry(errors={requests.exceptions.HTTPError}, error_codes={409})
    def test_dockstore_import_in_terra(self):
        # import the workflow into terra
        response = import_dockstore_wf_into_terra()
        method_info = response['methodConfiguration']['methodRepoMethod']
        with self.subTest('Dockstore Import Response: sourceRepo'):
            self.assertEqual(method_info['sourceRepo'], 'dockstore')
        with self.subTest('Dockstore Import Response: methodPath'):
            self.assertEqual(method_info['methodPath'], 'github.com/DataBiosphere/topmed-workflows/UM_aligner_wdl')
        with self.subTest('Dockstore Import Response: methodVersion'):
            self.assertEqual(method_info['methodVersion'], '1.32.0')

        # check that a second attempt gives a 409 error
        try:
            import_dockstore_wf_into_terra()
        except requests.exceptions.HTTPError as e:
            with self.subTest('Dockstore Import Response: 409 conflict'):
                self.assertEqual(e.response.status_code, 409)

        # check status that the workflow is seen in terra
        wf_seen_in_terra = False
        response = check_workflow_presence_in_terra_workspace()
        for wf_response in response:
            method_info = wf_response['methodRepoMethod']
            if method_info['methodPath'] == 'github.com/DataBiosphere/topmed-workflows/UM_aligner_wdl' \
                    and method_info['sourceRepo'] == 'dockstore' \
                    and method_info['methodVersion'] == '1.32.0':
                wf_seen_in_terra = True
                break
        with self.subTest('Dockstore Check Workflow Seen'):
            self.assertTrue(wf_seen_in_terra)

        # delete the workflow
        delete_workflow_presence_in_terra_workspace()

        # check status that the workflow is no longer seen in terra
        wf_seen_in_terra = False
        response = check_workflow_presence_in_terra_workspace()
        for wf_response in response:
            method_info = wf_response['methodRepoMethod']
            if method_info['methodPath'] == 'github.com/DataBiosphere/topmed-workflows/UM_aligner_wdl' \
                    and method_info['sourceRepo'] == 'dockstore' \
                    and method_info['methodVersion'] == '1.32.0':
                wf_seen_in_terra = True
                break
        with self.subTest('Dockstore Check Workflow Not Seen'):
            self.assertFalse(wf_seen_in_terra)

    @unittest.skip('This test needs to be updated.')
    def test_drs_workflow_in_terra(self):
        """This test runs md5sum in a fixed workspace using a drs url from gen3."""
        response = run_workflow()
        status = response['status']
        with self.subTest('Dockstore Workflow Run Submitted'):
            self.assertEqual(status, 'Submitted')
        with self.subTest('Dockstore Workflow Run Responds with DRS.'):
            self.assertTrue(response['workflows'][0]['inputResolutions'][0]['value'].startswith('drs://'))

        submission_id = response['submissionId']

        # md5sum should run for about 4 minutes, but may take far longer(?); give a generous timeout
        # also configurable manually via MD5SUM_TEST_TIMEOUT if held in a pending state
        start = time.time()
        deadline = start + int(os.environ.get('MD5SUM_TEST_TIMEOUT', 60 * 60))
        table = f'platform-dev-178517.bdc.terra_md5_latency_min_{STAGE}'
        while True:
            response = check_workflow_status(submission_id=submission_id)
            status = response['status']
            if response['workflows'][0]['status'] == "Failed":
                log_duration(table, time.time() - start)
                raise RuntimeError(f'The md5sum workflow did not succeed:\n{json.dumps(response, indent=4)}')
            elif status == 'Done':
                break
            else:
                now = time.time()
                if now < deadline:
                    print(f"md5sum workflow state is: {response['workflows'][0]['status']}. "
                          f"Checking again in 20 seconds.")
                    time.sleep(20)
                else:
                    print(json.dumps(response, indent=4))
                    log_duration(table, time.time() - start)
                    raise RuntimeError('The md5sum workflow run timed out.  '
                                       f'Expected 4 minutes, but took longer than '
                                       f'{float(now - start) / 60.0} minutes.')

        log_duration(table, time.time() - start)
        with self.subTest('Dockstore Workflow Run Completed Successfully'):
            if response['workflows'][0]['status'] != "Succeeded":
                raise RuntimeError(f'The md5sum workflow did not succeed:\n{json.dumps(response, indent=4)}')

    def test_pfb_handoff_from_gen3_to_terra(self):
        time_stamp = datetime.datetime.now().strftime("%Y_%m_%d_%H%M%S")
        workspace_name = f'integration_test_pfb_gen3_to_terra_{time_stamp}_delete_me'

        with self.subTest('Create a terra workspace.'):
            response = create_terra_workspace(workspace=workspace_name)
            self.assertTrue('workspaceId' in response)
            self.assertTrue(response['createdBy'] == 'biodata.integration.test.mule@gmail.com')

        with self.subTest('Import static pfb into the terra workspace.'):
            response = import_pfb(workspace=workspace_name,
                                  pfb_file='https://cdistest-public-test-bucket.s3.amazonaws.com/export_2020-06-02T17_33_36.avro')
            self.assertTrue('jobId' in response)

        with self.subTest('Check on the import static pfb job status.'):
            response = pfb_job_status_in_terra(workspace=workspace_name, job_id=response['jobId'])
            # this should take < 60 seconds
            while response['status'] in ['Translating', 'ReadyForUpsert', 'Upserting', 'Pending']:
                time.sleep(2)
                response = pfb_job_status_in_terra(workspace=workspace_name, job_id=response['jobId'])
            self.assertTrue(response['status'] == 'Done',
                            msg=f'Expecting status: "Done" but got "{response["status"]}".\n'
                                f'Full response: {json.dumps(response, indent=4)}')

        with self.subTest('Delete the terra workspace.'):
            response = delete_terra_workspace(workspace=workspace_name)
            if not response.ok:
                raise RuntimeError(
                    f'Could not delete the workspace "{workspace_name}": [{response.status_code}] {response}')
            if response.status_code != 202:
                logger.critical(f'Response {response.status_code} has changed: {response}')
            response = delete_terra_workspace(workspace=workspace_name)
            self.assertTrue(response.status_code == 404)

    @staging_only
    def test_public_data_access(self):
        # this DRS URI only exists on staging/alpha and requires os.environ['TERRA_DEPLOYMENT_ENV'] = 'alpha'
        drs.head('drs://dg.712C/fa640b0e-9779-452f-99a6-16d833d15bd0',
                 workspace_name='DRS-Test-Workspace', workspace_namespace=BILLING_PROJECT)

    @unittest.skip('This test needs to be updated.')
    def test_controlled_data_access(self):
        # this DRS URI only exists on staging/alpha and requires os.environ['TERRA_DEPLOYMENT_ENV'] = 'alpha'
        drs.head('drs://dg.712C/04fbb96d-68c9-4922-801e-9b1350be3b94',
                 workspace_name='DRS-Test-Workspace', workspace_namespace=BILLING_PROJECT)

    @unittest.skip('Website syntax has changed.  This test needs to be updated.')
    @staging_only
    def test_selenium_RAS_login(self):
        from selenium import webdriver
        from selenium.webdriver.common.keys import Keys
        from selenium.common.exceptions import NoSuchElementException

        options = webdriver.FirefoxOptions()
        options.add_argument("--headless")
        driver = webdriver.Firefox(options=options)
        driver.get(
            "https://staging.gen3.biodatacatalyst.nhlbi.nih.gov/user/oauth2/authorize?"
            "response_type=code&"
            "client_id=4EmZnWKVMoPyhdJMh7EB8SSl3Uojo20QfsAR77gu&"
            "redirect_uri=https%3A%2F%2Falpha.terra.biodatacatalyst.nhlbi.nih.gov%2F%23fence-callback&"
            "scope=openid+google_credentials+data+user&"
            "idp=ras"
        )

        if driver.title != 'Sign In - NIH Login':
            print(f'Warning, the website title message has changed bro: {driver.title}')

        username_box = driver.find_element_by_name("USER")
        username_box.clear()
        username_box.send_keys(os.environ['RAS_USER'])

        password_box = driver.find_element_by_name("PASSWORD")
        password_box.clear()
        password_box.send_keys(base64.decodebytes(os.environ['RASP'].encode('utf-8')).decode('utf-8'))
        password_box.send_keys(Keys.RETURN)

        time.sleep(5)  # let the page load...
        timeout = 60
        while timeout > 0:
            try:
                accept_button = driver.find_element_by_xpath('//button[normalize-space()="Yes, I authorize."]')
                accept_button.click()
                break
            except NoSuchElementException:
                time.sleep(2)
                timeout -= 2

        time.sleep(5)  # let the page load...
        timeout = 60
        while timeout > 0:
            try:
                # if this exists, we've successfully logged in
                driver.find_element_by_xpath('//h1[normalize-space()="Welcome to NHLBI BioData Catalyst"]')
                break
            except NoSuchElementException:
                time.sleep(2)
                timeout -= 2

    @staging_only
    def test_import_drs_from_gen3(self):
        # TODO: This commented out section SHOULD be how we check for the ACL and DRS files we don't
        #  have access to, but this is giving problems, so have to hardcode known restricted files.
        # public_uris = ['drs://dg.712C/60df2552-3d67-4f21-b637-5b53684fe444',
        #                'drs://dg.712C/a83118b2-8fdc-42de-9de2-5ddb27efb8eb',
        #                'drs://dg.712C/a84c1bcf-1975-402f-a973-ff7a307a2e90',
        #                'drs://dg.712C/b7a10338-6fb6-4201-adde-0ee933e069bc',
        #                'drs://dg.712C/2d9692bb-2050-4742-b7ae-42a83de6129e',
        #                'drs://dg.712C/39474412-fc6d-4dbc-81c2-176db2403130']
        #
        # response = requests.get(
        #     'https://staging.gen3.biodatacatalyst.nhlbi.nih.gov/index/index?'
        #     'negate_params={"acl":["*","admin","topmed",'
        #     '"phs000888", "phs000681", "phs001014", "phs001095", "phs001215", '
        #     '"phs001544", "phs001395", "phs000169", "phs000636", "phs000820", '
        #     '"phs000971", "phs000984", "phs000292", "phs000997", "phs000944", '
        #     '"phs000304", "phs000209", "phs000538", "phs000353"]}')
        # restricted_records = [r['did'] for r in response.json()['records'] if r['did'] not in public_uris]
        # drs_uri = random.choice(restricted_records)

        # first try to download the file and we should be denied
        # only downloads the first byte even if successful to keep it short
        response = import_drs_with_direct_gen3_access_token('drs://dg.712C/01229405-6ce4-4ad7-aa04-19124afadebc')
        self.assertEqual(response.status_code, 401)  # not a 403?

    # @staging_only
    # def test_import_drs_from_gen3(self):
    #     # file is ~1gb, so only download the first byte to check for access
    #     import_drs_from_gen3('drs://dg.712C/95dc0845-d895-489f-aaf8-583a676037f7')
    #
    #     # TODO: Investigate the following:
    #     # the following file is 5b, but we get a "Not enough segments" Error, so there may be problems with small files:
    #     # <p class="body">Error Message:</p>\n          <p class="introduction">Not enough segments</p>\n          \n          <div>\n            \n            <p class="body">Please try again!</p>
    #     # import_drs_from_gen3('drs://dg.712C/b7a10338-6fb6-4201-adde-0ee933e069bc')


class SaveResult(TextTestResult):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tests_run: Dict[unittest.TestCase, str] = {}

    def startTest(self, test):
        super().startTest(test)
        self.tests_run[test] = 'started'

    def addSuccess(self, test) -> None:
        super().addSuccess(test)
        self.tests_run[test] = 'success'

    def addFailure(self, test, err) -> None:
        super().addFailure(test, err)
        self.tests_run[test] = 'failure'

    def addError(self, test, err) -> None:
        super().addError(test, err)
        self.tests_run[test] = 'error'

    def addSkip(self, test, reason) -> None:
        super().addSkip(test, reason)
        self.tests_run[test] = 'skip'

    def addUnexpectedSuccess(self, test) -> None:
        super().addUnexpectedSuccess(test)
        self.tests_run[test] = 'failure'

    def addExpectedFailure(self, test, err) -> None:
        super().addExpectedFailure(test, err)
        self.tests_run[test] = 'success'


class SaveResultRunner(TextTestRunner):
    resultclass = SaveResult


if __name__ == "__main__":
    test_run = unittest.main(verbosity=2, exit=False, testRunner=SaveResultRunner)
    results: SaveResult = test_run.result
    timestamp = datetime.datetime.now()
    client = Client()
    for test, status in results.tests_run.items():
        # Unfortunately this is the only way to get the test method name from the TestCase
        test_name = test._testMethodName
        try:
            # To create tables, skip all tests and set create to True:
            client.log_test_results(test_name, status, timestamp, create=True)
        except Exception as e:
            logger.exception('Failed to log test %r', test, exc_info=e)
    sys.exit(not results.wasSuccessful())
