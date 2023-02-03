from django.conf import settings
from django.core.mail import send_mail

import requests

REST_URL = '/services/data/%d.0/' % settings.SALESFORCE_API_VERSION
TOOLING_URL = '%s/tooling/' % REST_URL


def get_subdomain(environment):
    """
    Get the Salesforce sub-domain based on Production or Test
    """
    return 'login' if environment == 'Production' else 'test'


def get_headers(access_token):
    """
    Build the headers for each authorised request
    """
    return {
        'Authorization': 'Bearer %s' % access_token,
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }

def get_user(instance_url, access_token, user_id):
    """
    Get the Salesforce Username
    """

    # Build the URL
    url = '%s%ssobjects/User/%s' % (instance_url, settings.SALESFORCE_REST_URL, user_id)
    print(url)
    # Query for the user record
    result = requests.get(url, headers=get_headers(access_token))

    return result.json()

def get_user_with_no_id(instance_url, access_token):
    """
    Get the Salesforce user details without knowing the user id
    """
    # Build the URL
    url = '%s%schatter/users/me' % (instance_url, settings.SALESFORCE_REST_URL)

    # Query for the user record
    result = requests.get(url, headers=get_headers(access_token))
    
    return result.json()


def send_finished_email(job):
    """
    Send email notifying of finished job
    """

    if job.status == 'Error':

        subject = 'There was an error running for Apex Code Scan'
        email_body = job.error

    else:

        subject = 'Your Salesforce Apex Code Scan results are ready.'
        email_body = 'Your Salesforce Apex Code Scan job is complete:\n'
        email_body += 'https://sfcodeclean.herokuapp.com/job/' + job.slug
        email_body += '\n\nYour result will be deleted after one day in order to avoid storing any metadata.'

    send_mail(
        subject, 
        email_body, 
        settings.DEFAULT_FROM_EMAIL, 
        [job.email], 
        fail_silently=True
    )

