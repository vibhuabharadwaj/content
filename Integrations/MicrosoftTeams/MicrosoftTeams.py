import demistomock as demisto
from CommonServerPython import *
from CommonServerUserPython import *
''' IMPORTS '''
import requests
from distutils.util import strtobool
from flask import Flask, request, Response
from gevent.pywsgi import WSGIServer
import time
from threading import Thread
from typing import Match, Union, Optional, cast, Dict, Any, List
import re

# Disable insecure warnings
requests.packages.urllib3.disable_warnings()

''' GLOBAL VARIABLES'''
PARAMS = demisto.params()
BOT_ID = PARAMS.get('bot_id')
BOT_PASSWORD = PARAMS.get('bot_password')
TEAM = PARAMS.get('team')
USE_SSL = not PARAMS.get('insecure', False)
APP = Flask('demisto-teams')
PLAYGROUND_INVESTIGATION_TYPE: int = 9
GRAPH_BASE_URL: str = 'https://graph.microsoft.com'

INCIDENT_TYPE: str = PARAMS.get('incidentType', '')

URL_REGEX: str = r'http[s]?://(?:[a-zA-Z]|[0-9]|[:/$_@.&+#-]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
ENTITLEMENT_REGEX: str = r'(\{){0,1}[0-9a-fA-F]{8}\-[0-9a-fA-F]{4}\-[0-9a-fA-F]{4}\-[0-9a-fA-F]{4}\-[0-9a-fA-F]{12}(\}){0,1}'
ENTRY_FOOTER: str = 'From Microsoft Teams'

MESSAGE_TYPES: dict = {
    'mirror_entry': 'mirrorEntry',
    'incident_opened': 'incidentOpened',
    'status_changed': 'incidentStatusChanged'
}

''' HELPER FUNCTIONS '''


def epoch_seconds(d: datetime = None) -> int:
    """
    Return the number of seconds for given date. If no date, return current.

    Args:
        d (datetime): timestamp
    Returns:
         int: timestamp in epoch
    """
    if not d:
        d = datetime.utcnow()
    return int((d - datetime.utcfromtimestamp(0)).total_seconds())


def error_parser(resp_err: requests.Response) -> str:
    """
    Parses error message from Requests response
    :param resp_err: response with error
    :return: string of error
    """
    try:
        response = resp_err.json()
        error = response.get('error', {})
        err_str = f"{error.get('code')}: {error.get('message')}"
        if err_str:
            return err_str
        # If no error message
        raise ValueError
    except ValueError:
        return resp_err.text


def translate_severity(severity: str) -> int:
    """
    Translates Demisto text severity to int severity
    :param severity: Demisto text severity
    :return: Demisto integer severity
    """
    severity_dictionary = {
        'Low': 1,
        'Medium': 2,
        'High': 3,
        'Critical': 4
    }
    return severity_dictionary.get(severity, 0)


def create_incidents(demisto_user: dict, incidents: list) -> dict:
    """
    Creates incidents according to a provided JSON object
    :param demisto_user: The demisto user associated with the request (if exists)
    :param incidents: The incidents JSON
    :return: The creation result
    """
    if demisto_user:
        data = demisto.createIncidents(incidents, userID=demisto_user['id'])
    else:
        data = demisto.createIncidents(incidents)
    return data


def process_incident_create_message(demisto_user: dict, message: str) -> str:
    """
    Processes an incident creation message
    :param demisto_user: The Demisto user associated with the message (if exists)
    :param message: The creation message
    :return: Creation result
    """
    json_pattern: str = r'(?<=json=).*'
    name_pattern: str = r'(?<=name=).*'
    type_pattern: str = r'(?<=type=).*'
    json_match: Optional[Match[str]] = re.search(json_pattern, message)
    created_incident = None
    data: str = ''
    if json_match:
        if re.search(name_pattern, message) or re.search(type_pattern, message):
            data = 'No other properties other than json should be specified.'
        else:
            incidents_json = json_match.group()
            incidents = json.loads(incidents_json.replace('“', '"').replace('”', '"'))
            if not isinstance(incidents, list):
                incidents = [incidents]
            created_incident = create_incidents(demisto_user, incidents)
            if not created_incident:
                data = 'Failed creating incidents.'
    else:
        name_match = re.search(name_pattern, message)
        if not name_match:
            data = 'Please specify arguments in the following manner: name=<name> type=[type] or json=<json>.'
        else:
            incident_name = re.sub('type=.*', '', name_match.group()).strip()
            incident_type: str = ''

            type_match = re.search(type_pattern, message)
            if type_match:
                incident_type = re.sub('name=.*', '', type_match.group()).strip()

            incident = {'name': incident_name}

            incident_type = incident_type or INCIDENT_TYPE
            if incident_type:
                incident['type'] = incident_type

            created_incident = create_incidents(demisto_user, [incident])
            if not created_incident:
                data = 'Failed creating incidents.'

    if created_incident:
        if isinstance(created_incident, list):
            created_incident = created_incident[0]
        server_links = demisto.demistoUrls()
        server_link = server_links.get('server')
        data = ('Successfully created incident {}.\n View it on: {}#/WarRoom/{}'
                .format(created_incident['name'], server_link, created_incident['id']))

    return data


def is_investigation_mirrored(investigation_id: str, mirrored_channels: list) -> int:
    for index, channel in enumerate(mirrored_channels):
        if channel.get('investigation_id') == investigation_id:
            return index
    return -1


def urlify_hyperlinks(message: str) -> str:
    formatted_message: str = message
    # URLify markdown hyperlinks
    urls = re.findall(URL_REGEX, message)
    for url in urls:
        formatted_message = formatted_message.replace(url, f'[{url}]({url})')
    return formatted_message


def get_team_member(integration_context: dict, user_id: str) -> dict:
    team_member: dict = dict()
    teams: list = json.loads(integration_context.get('teams', '[]'))
    for team in teams:
        team_members: list = team.get('team_members', [])
        for member in team_members:
            if member.get('id') == user_id:
                team_member['username'] = member.get('name', '')
                team_member['user_email'] = member.get('userPrincipalName', '')
                break
        if team_member:
            break
    if not team_member:
        raise ValueError('Team member was not found')
    return team_member


def get_team_member_id(requested_team_member: str, integration_context: dict) -> str:
    member_id: str = str()
    teams: list = json.loads(integration_context.get('teams', '[]'))
    for team in teams:
        team_members: list = team.get('team_members', [])
        for team_member in team_members:
            if requested_team_member in {team_member.get('name', ''), team_member.get('userPrincipalName', '')}:
                member_id = team_member.get('id')
                break
        if member_id:
            break
    if not member_id:
        raise ValueError('Team member was not found')
    return member_id


def create_adaptive_card(body: list, actions: list = None) -> dict:
    adaptive_card: dict = {
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.0",
            "type": "AdaptiveCard",
            "body": body
        }
    }
    if actions:
        adaptive_card['content']['actions'] = actions
    return adaptive_card


def process_tasks_list(data_by_line: list) -> dict:
    body: list = list()
    for line in data_by_line[2:]:
        split_data: list = [stat.strip() for stat in line.split('|')]
        body.append({
            'type': 'FactSet',
            'facts': [
                {
                    'title': 'Task:',
                    'value': split_data[0]
                },
                {
                    'title': 'Incident:',
                    'value': split_data[1]
                },
                {
                    'title': 'Due:',
                    'value': split_data[2]
                },
                {
                    'title': 'Link:',
                    'value': f'[{split_data[3]}]({split_data[3]})'
                }
            ]
        })
    return create_adaptive_card(body)


def process_incidents_list(data_by_line: list) -> dict:
    body: list = list()
    for line in data_by_line[2:]:
        split_data: list = [stat.strip() for stat in line.split('|')]
        body.append({
            'type': 'FactSet',
            'facts': [
                {
                    'title': 'ID:',
                    'value': split_data[0]
                },
                {
                    'title': 'Name:',
                    'value': split_data[1]
                },
                {
                    'title': 'Status:',
                    'value': split_data[2]
                },
                {
                    'title': 'Type:',
                    'value': split_data[3]
                },
                {
                    'title': 'Owner:',
                    'value': split_data[4]
                },
                {
                    'title': 'Created:',
                    'value': split_data[5]
                },
                {
                    'title': 'Link:',
                    'value': f'[{split_data[6]}]({split_data[6]})'
                }
            ]
        })
    return create_adaptive_card(body)


def process_unknown_message(message: str) -> dict:
    body: list = [{
        'type': 'TextBlock',
        'text': message.replace('\n', '\n\n'),
        'wrap': True
    }]
    return create_adaptive_card(body)


def process_ask_user(message: str) -> dict:
    message_object: dict = json.loads(message)
    text: str = message_object.get('message_text', '')
    entitlement: str = message_object.get('entitlement', '')
    options: list = message_object.get('options', [])
    investigation_id: str = message_object.get('investigation_id', '')
    task_id: str = message_object.get('task_id', '')
    body = [
        {
            'type': 'TextBlock',
            'text': text
        }
    ]
    actions: list = list()
    for option in options:
        actions.append({
            'type': 'Action.Submit',
            'title': option,
            'data': {
                'response': option,
                'entitlement': entitlement,
                'investigation_id': investigation_id,
                'task_id': task_id
            }
        })
    return create_adaptive_card(body, actions)


def get_bot_access_token() -> str:
    integration_context: dict = demisto.getIntegrationContext()
    access_token: str = integration_context.get('bot_access_token', '')
    valid_until: int = integration_context.get('bot_valid_until', int)
    if access_token and valid_until:
        if epoch_seconds() < valid_until:
            return access_token
    url: str = 'https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token'
    data: dict = {
        'grant_type': 'client_credentials',
        'client_id': BOT_ID,
        'client_secret': BOT_PASSWORD,
        'scope': 'https://api.botframework.com/.default'
    }
    response: requests.Response = requests.post(
        url,
        data=data,
        verify=USE_SSL
    )
    if not response.ok:
        error = error_parser(response)
        raise ValueError(f'Failed to get bot access token [{response.status_code}] - {error}')
    try:
        response_json: dict = response.json()
        access_token = response_json.get('access_token', '')
        expires_in: int = response_json.get('expires_in', 3595)
        time_now: int = epoch_seconds()
        time_buffer = 5  # seconds by which to shorten the validity period
        if expires_in - time_buffer > 0:
            expires_in -= time_buffer
        integration_context['bot_access_token'] = access_token
        integration_context['bot_valid_until'] = time_now + expires_in
        return access_token
    except ValueError:
        raise ValueError('Failed to get bot access token')


def get_graph_access_token() -> str:
    integration_context: dict = demisto.getIntegrationContext()
    access_token: str = integration_context.get('graph_access_token', '')
    valid_until: int = integration_context.get('graph_valid_until', int)
    if access_token and valid_until:
        if epoch_seconds() < valid_until:
            return access_token
    tenant_id: str = integration_context.get('tenant_id', '')
    if not tenant_id:
        raise ValueError('Tenant ID not found')
    url: str = f'https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token'
    data: dict = {
        'grant_type': 'client_credentials',
        'client_id': BOT_ID,
        'scope': 'https://graph.microsoft.com/.default',
        'client_secret': BOT_PASSWORD
    }

    response: requests.Response = requests.post(
        url,
        data=data,
        verify=USE_SSL
    )

    if not response.ok:
        error = error_parser(response)
        raise ValueError(f'Failed to get Graph access token [{response.status_code}] - {error}')
    try:
        response_json: dict = response.json()
        access_token = response_json.get('access_token', '')
        expires_in: int = response_json.get('expires_in', 3595)
        time_now: int = epoch_seconds()
        time_buffer = 5  # seconds by which to shorten the validity period
        if expires_in - time_buffer > 0:
            expires_in -= time_buffer
        integration_context['graph_access_token'] = access_token
        integration_context['graph_valid_until'] = time_now + expires_in
        return access_token
    except ValueError:
        raise ValueError('Failed to get Graph access token')


def http_request(
        method: str, url: str = '', data: dict = None, _json: dict = None, api: str = 'graph'
) -> Union[dict, list]:
    """
    A wrapper for requests lib to send our requests and handle requests and responses better
    Headers to be sent in requests

    Args:
        method (str): any restful method
        url (str): URL to query
        data (dict): HTTP body
        _json (dict): HTTP JSON body
        api (str): API to query (graph/bot)

    Returns:
        dict: requests.json()
    """
    if api == 'graph':
        access_token = get_graph_access_token()
    else:  # Bot Framework API
        access_token = get_bot_access_token()

    headers: dict = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }

    response: requests.Response = requests.request(
        method,
        url,
        verify=USE_SSL,
        data=data,
        headers=headers,
        json=_json
    )

    if not response.ok:
        return_error(response.text)
        error = error_parser(response)
        raise ValueError(f'Error in API call to Microsoft Teams: [{response.status_code}] - {error}')
    demisto.debug(response.status_code)
    if response.status_code in {202, 204}:
        return {}
    if response.status_code == 201:
        # For channel creation query, we get a body in the response, otherwise we should just return
        if not response.content:
            return {}
    try:
        return response.json()
    except ValueError:
        raise ValueError('Could not decode response from API')


''' COMMANDS + REQUESTS FUNCTIONS '''


def get_team_aad_id(team_name: str) -> str:
    """
    Gets Team AAD ID
    :param team_name: Team name to get AAD ID of
    :return: team AAD ID
    """
    integration_context: dict = demisto.getIntegrationContext()
    if integration_context.get('teams'):
        teams: list = json.loads(integration_context['teams'])
        for team in teams:
            if team_name == team.get('team_name', ''):
                return team.get('team_aad_id', '')
    url: str = f"{GRAPH_BASE_URL}/beta/groups?$filter=resourceProvisioningOptions/Any(x:x eq 'Team')"
    response: dict = cast(Dict[Any, Any], http_request('GET', url))
    teams = response.get('value', [])
    for team in teams:
        if team.get('displayName', '') == team_name:
            return team.get('id', '')
    raise ValueError('Could not find requested team.')


# def add_member_to_team(user_principal_name: str, team_id: str):
#     url: str = f'{GRAPH_BASE_URL}/v1.0/groups/{team_id}/members/$ref'
#     request_json: dict = {
#         '@odata.id': f'https://graph.microsoft.com/v1.0/directoryObjects/{user_principal_name}'
#     }
#     http_request('POST', url, _json=request_json)


def get_users() -> list:
    url: str = f'{GRAPH_BASE_URL}/v1.0/users'
    users: dict = cast(Dict[Any, Any], http_request('GET', url))
    return users.get('value', [])


def create_group_request(
        display_name: str, mail_enabled: bool, mail_nickname: str, security_enabled: bool,
        owners_ids: list, members_ids: list = None
) -> str:
    url = f'{GRAPH_BASE_URL}/v1.0/groups'
    data: dict = {
        'displayName': display_name,
        'groupTypes': ['Unified'],
        'mailEnabled': mail_enabled,
        'mailNickname': mail_nickname,
        'securityEnabled': security_enabled,
        'owners@odata.bind': owners_ids,
        'members@odata.bind': members_ids or owners_ids
    }
    group_creation_response: dict = cast(Dict[Any, Any], http_request('POST', url, _json=data))
    group_id: str = group_creation_response.get('id', '')
    return group_id


def create_team_request(group_id: str) -> str:
    url = f'{GRAPH_BASE_URL}/v1.0/groups/{group_id}/team'
    team_creation_response: dict = cast(Dict[Any, Any], http_request('PUT', url, _json={}))
    team_id: str = team_creation_response.get('id', '')
    return team_id


def add_bot_to_team(team_id: str):
    url: str = f'{GRAPH_BASE_URL}/v1.0/teams/{team_id}/installedApps'
    bot_app_id: str = ''
    data: dict = {
        'teamsApp@odata.bind': f'https://graph.microsoft.com/v1.0/appCatalogs/teamsApps/{bot_app_id}'
    }
    print(http_request('POST', url, _json=data))


def create_team():
    display_name: str = demisto.args().get('display_name', '')
    mail_enabled: bool = bool(strtobool(demisto.args().get('mail_enabled', True)))
    mail_nickname: str = demisto.args().get('mail_nickname', '')
    security_enabled: bool = bool(strtobool(demisto.args().get('security_enabled', True)))
    owners = argToList(demisto.args().get('owner', ''))
    members = argToList(demisto.args().get('members', ''))
    owners_ids: list = list()
    members_ids: list = list()
    users: list = get_users()
    user_id: str = str()
    for member in members:
        found_member: bool = False
        for user in users:
            if member in {user.get('displayName', ''), user.get('mail'), user.get('userPrincipalName')}:
                found_member = True
                user_id = user.get('id', '')
                members_ids.append(f'https://graph.microsoft.com/v1.0/users/{user_id}')
                break
        if not found_member:
            demisto.results({
                'Type': entryTypes['warning'],
                'Contents': f'User {member} was not found',
                'ContentsFormat': formats['text']
            })
    for owner in owners:
        found_owner: bool = False
        for user in users:
            if owner in {user.get('displayName', ''), user.get('mail'), user.get('userPrincipalName')}:
                found_owner = True
                user_id = user.get('id', '')
                owners_ids.append(f'https://graph.microsoft.com/v1.0/users/{user_id}')
                break
        if not found_owner:
            demisto.results({
                'Type': entryTypes['warning'],
                'Contents': f'User {owner} was not found',
                'ContentsFormat': formats['text']
            })
    if not owners_ids:
        raise ValueError('Could not find given users to be Team owners.')
    group_id: str = create_group_request(
        display_name, mail_enabled, mail_nickname, security_enabled, owners_ids, members_ids
    )
    team_id: str = create_team_request(group_id)
    add_bot_to_team(team_id)
    demisto.results(f'Team {display_name} was created successfully')


def create_channel(team_aad_id: str, channel_name: str, channel_description: str = '') -> str:
    url: str = f'{GRAPH_BASE_URL}/v1.0/teams/{team_aad_id}/channels'
    request_json: dict = {
        'displayName': channel_name,
        'description': channel_description
    }
    channel_data: dict = cast(Dict[Any, Any], http_request('POST', url, _json=request_json))
    assert isinstance(channel_data, dict)
    channel_id: str = channel_data.get('id', '')
    return channel_id


def get_channel_id(channel_name: str, team_aad_id: str) -> str:
    integration_context: dict = demisto.getIntegrationContext()
    if integration_context.get('teams'):
        teams: list = json.loads(integration_context['teams'])
        for team in teams:
            mirrored_channels: list = team.get('mirrored_channels', [])
            for channel in mirrored_channels:
                investigation_id: str = channel.get('investigation_id', '')
                if channel_name == f'incident-{investigation_id}':
                    return channel.get('channel_id')
    url: str = f'{GRAPH_BASE_URL}/v1.0/teams/{team_aad_id}/channels'
    response: dict = cast(Dict[Any, Any], http_request('GET', url))
    channel_id: str = ''
    channels: list = response.get('value', [])
    for channel in channels:
        channel_display_name: str = channel.get('displayName', '')
        if channel_display_name == channel_name:
            channel_id = channel.get('id', '')
            break
    if not channel_id:
        raise ValueError(f'Could not find channel: {channel_name}')
    return channel_id


def get_team_members(team_id: str, service_url: str) -> list:
    url: str = f'{service_url}/v3/conversations/{team_id}/members'
    response: list = cast(List[Any], http_request('GET', url, api='bot'))
    return response


def update_message(service_url: str, conversation_id: str, activity_id: str, text: str):
    body = [{
        'type': 'TextBlock',
        'text': text
    }]
    adaptive_card: dict = create_adaptive_card(body=body)
    conversation = {
        'type': 'message',
        'attachments': [adaptive_card]
    }
    url: str = f'{service_url}/v3/conversations/{conversation_id}/activities/{activity_id}'
    http_request('PUT', url, _json=conversation, api='bot')


def close_channel_request(team_aad_id: str, channel_id: str):
    url: str = f'{GRAPH_BASE_URL}/v1.0/teams/{team_aad_id}/channels/{channel_id}'
    http_request('DELETE', url)


def close_channel():
    """
    Deletes a mirrored Teams channel
    """
    integration_context: dict = demisto.getIntegrationContext()
    channel_name: str = demisto.args().get('channel', '')
    channel_id: str = str()
    team_aad_id: str
    mirrored_channels: list = list()
    if not channel_name:
        # Closing channel as part of autoclose in mirroring process
        investigation: dict = demisto.investigation()
        investigation_id: str = investigation.get('id', '')
        teams: list = json.loads(integration_context.get('teams', '[]'))
        for team_index, team in enumerate(teams):
            team_aad_id = team.get('team_aad_id', '')
            mirrored_channels = team.get('mirrored_channels', [])
            for channel_index, channel in enumerate(mirrored_channels):
                if channel.get('investigation_id') == investigation_id:
                    channel_id = channel.get('channel_id', '')
                    close_channel_request(team_aad_id, channel_id)
                    team_to_update: dict = teams.pop(team_index)
                    mirrored_channels.pop(channel_index)
                    team_to_update['mirrored_channels'] = mirrored_channels
                    teams.append(team_to_update)
                    break
        if not channel_id:
            raise ValueError('Could not find mirrored Teams channel to close.')
        integration_context['teams'] = json.dumps(teams)
        demisto.setIntegrationContext(integration_context)
    else:
        team_name: str = demisto.args().get('team') or demisto.params().get('team')
        team_aad_id = get_team_aad_id(team_name)
        channel_id = get_channel_id(channel_name, team_aad_id)
        close_channel_request(team_aad_id, channel_id)
    demisto.results('Channel was successfully closed')


def create_personal_conversation(integration_context: dict, member_id: str) -> str:
    bot_id: str = demisto.params().get('bot_id', '')
    bot_name: str = integration_context.get('bot_name', '')
    tenant_id: str = integration_context.get('tenant_id', '')
    conversation: dict = {
        'bot': {
            'id': f'28:{bot_id}',
            'name': bot_name
        },
        'members': [{
            'id': member_id
        }],
        'channelData': {
            'tenant': {
                'id': tenant_id
            }
        }
    }
    service_url: str = integration_context.get('service_url', '')
    if not service_url:
        raise ValueError('Did not find service URL. Try messaging the bot on Microsoft Teams')
    url: str = f'{service_url}/v3/conversations'
    response: dict = cast(Dict[Any, Any], http_request('POST', url, _json=conversation, api='bot'))
    return response.get('id', '')


def send_message_request(channel_id: str, conversation: dict, integration_context: dict = {}):
    service_url: str = integration_context.get('service_url', '')
    if not service_url:
        raise ValueError('Did not find service URL. Try messaging the bot on Microsoft Teams')
    url: str = f'{service_url}/v3/conversations/{channel_id}/activities'
    http_request('POST', url, _json=conversation, api='bot')


def send_message():
    message_type: str = demisto.args().get('messageType', '')
    original_message: str = demisto.args().get('originalMessage', '')
    message: str = demisto.args().get('message', '')

    if message_type == MESSAGE_TYPES['mirror_entry'] and ENTRY_FOOTER in original_message:
        # Got a message which was already mirrored - skipping it
        return
    channel_name: str = demisto.args().get('channel', '')

    if not channel_name and message_type in {MESSAGE_TYPES['status_changed'], MESSAGE_TYPES['incident_opened']}:
        # Got a notification from server
        channel_name = demisto.params().get('incident_notifications_channel', 'General')
        severity: int = int(demisto.args().get('severity'))
        severity_threshold: int = translate_severity(demisto.params().get('min_incident_severity', 'Low'))
        if severity < severity_threshold:
            return

    team_member: str = demisto.args().get('team_member', '')

    if not (team_member or channel_name):
        raise ValueError('No channel or user to send message were provided.')

    if team_member and channel_name:
        raise ValueError('Provide either channel or user to send message to, not both.')

    integration_context: dict = demisto.getIntegrationContext()
    channel_id: str = str()
    personal_conversation_id: str = str()
    if channel_name:
        team_name: str = demisto.args().get('team', '') or demisto.params().get('team', '')
        team_aad_id: str = get_team_aad_id(team_name)
        channel_id = get_channel_id(channel_name, team_aad_id)
    elif team_member:
        member_id: str = get_team_member_id(team_member, integration_context)
        personal_conversation_id = create_personal_conversation(integration_context, member_id)

    recipient: str = channel_id or personal_conversation_id

    entitlement_match: Optional[Match[str]] = re.search(ENTITLEMENT_REGEX, message)
    if entitlement_match:
        # In TeamsAskUser process
        adaptive_card = process_ask_user(message)
        conversation: dict = {
            'type': 'message',
            'attachments': [adaptive_card]
        }
    else:
        # Sending regular message
        formatted_message: str = urlify_hyperlinks(message)
        conversation = {
            'type': 'message',
            'text': formatted_message
        }

    send_message_request(recipient, conversation, integration_context)
    demisto.results('Message was sent successfully.')


def mirror_investigation():
    """
    Updates the integration context with a new or existing mirror.
    """

    investigation: dict = demisto.investigation()

    if investigation.get('type') == PLAYGROUND_INVESTIGATION_TYPE:
        raise ValueError('Can not perform this action in playground.')

    integration_context: dict = demisto.getIntegrationContext()

    mirror_type: str = demisto.args().get('mirror_type', 'all')
    auto_close: str = demisto.args().get('autoclose', 'true')
    mirror_direction: str = demisto.args().get('direction', 'both')
    team_name: str = demisto.args().get('team', '')
    if not team_name:
        team_name = demisto.params().get('team', '')
    team_aad_id: str = get_team_aad_id(team_name)
    team_to_mirror_channel: dict = dict()
    mirrored_channels: list = list()
    if integration_context.get('teams'):
        teams: list = json.loads(integration_context['teams'])
        for index, team in enumerate(teams):
            if team.get('team_aad_id', '') == team_aad_id:
                if team.get('mirrored_channels'):
                    mirrored_channels = team['mirrored_channels']
                team_to_mirror_channel = teams.pop(index)
    if mirror_direction != 'both':
        mirror_type = f'{mirror_type}:{mirror_direction}'

    investigation_id: str = investigation.get('id', '')
    investigation_mirrored_index = is_investigation_mirrored(investigation_id, mirrored_channels)

    if investigation_mirrored_index > -1:
        # Updating channel mirror configuration
        channel = mirrored_channels.pop(investigation_mirrored_index)
        mirrored_channels.append({
            'channel_id': channel.get('channel_id', ''),
            'investigation_id': investigation.get('id'),
            'mirror_type': mirror_type,
            'mirror_direction': mirror_direction,
            'auto_close': auto_close,
            'mirrored': False,
            'channel_name': channel.get('channel_name')
        })
        demisto.results('Investigation mirror was updated successfully')
    else:
        channel_name: str = f'incident-{investigation_id}'
        channel_description = f'Channel to mirror incident {investigation_id}'
        channel_id = create_channel(team_aad_id, channel_name, channel_description)
        mirrored_channels.append({
            'channel_id': channel_id,
            'investigation_id': investigation.get('id'),
            'mirror_type': mirror_type,
            'mirror_direction': mirror_direction,
            'auto_close': auto_close,
            'mirrored': False,
            'channel_name': channel_name
        })
        demisto.results(f'Investigation mirrored successfully in channel incident-{investigation_id}')

    team_to_mirror_channel['mirrored_channels'] = mirrored_channels
    teams.append(team_to_mirror_channel)
    integration_context['teams'] = json.dumps(teams)
    demisto.setIntegrationContext(integration_context)


def channel_mirror_loop():
    """
    Runs in a long running container - checking for newly mirrored investigations.
    """
    while True:
        try:
            integration_context = demisto.getIntegrationContext()
            if integration_context.get('mirrored_channels'):
                mirrored_channels = json.loads(integration_context['mirrored_channels'])
                for index, channel in enumerate(mirrored_channels):
                    investigation_id = channel.get('investigation_id', '')
                    if not channel['mirrored']:
                        demisto.info(f'Mirroring incident: {investigation_id} in Microsoft Teams')
                        channel = mirrored_channels.pop(index)
                        if channel['mirror_direction'] and channel['mirror_type']:
                            demisto.mirrorInvestigation(
                                channel['investigation_id'],
                                channel['mirror_type'],
                                bool(strtobool(channel['auto_close']))
                            )
                            channel['mirrored'] = True
                            mirrored_channels.append(channel)
                            demisto.info(f'Mirrored incident: {investigation_id} to Microsoft Teams successfully')
                        else:
                            demisto.info(f'Could not mirror {investigation_id}')
                        integration_context['mirrored_channels'] = json.dumps(mirrored_channels)
                        demisto.setIntegrationContext(integration_context)
        except Exception as e:
            demisto.updateModuleHealth(f'An error occurred: {str(e)}')
        finally:
            time.sleep(5)


def member_added_handler(request_body: dict, service_url: str, channel_data: dict):
    bot_id = demisto.params().get('bot_id')

    team: dict = channel_data.get('team', {})
    team_id: str = team.get('id', '')
    team_aad_id: str = team.get('aadGroupId', '')
    team_name: str = team.get('name', '')

    tenant: dict = channel_data.get('tenant', {})
    tenant_id: str = tenant.get('id', '')

    recipient: dict = request_body.get('recipient', {})
    recipient_name: str = recipient.get('name', '')

    members_added: list = request_body.get('membersAdded', [])

    teams: list = list()

    integration_context: dict = demisto.getIntegrationContext()
    if integration_context.get('teams'):
        teams = json.loads(integration_context['teams'])

    for member in members_added:
        member_id = member.get('id', '')
        if bot_id in member_id:
            # The bot was added to a team, caching team ID and team members
            integration_context['tenant_id'] = tenant_id
            integration_context['bot_name'] = recipient_name
            break
    team_members: list = get_team_members(team_id, service_url)
    teams.append({
        'team_aad_id': team_aad_id,
        'team_id': team_id,
        'team_name': team_name,
        'team_members': team_members
    })
    integration_context['teams'] = json.dumps(teams)
    demisto.setIntegrationContext(integration_context)


def direct_message_handler(integration_context: dict, request_body: dict, conversation: dict, message: str):
    """
    Handles a direct message sent to the bot
    :param integration_context:
    :param request_body:
    :param conversation:
    :param message:
    :return:
    """
    conversation_id: str = conversation.get('id', '')

    from_property: dict = request_body.get('from', {})
    user_id: str = from_property.get('id', '')

    team_member: dict = get_team_member(integration_context, user_id)

    username: str = team_member.get('username', '')
    user_email: str = team_member.get('user_email', '')

    formatted_message: str = str()

    attachment: dict = dict()

    return_card: bool = False

    allow_external_incidents_creation: bool = demisto.params().get('allow_external_incidents_creation', False)

    lowered_message = message.lower()
    if lowered_message.find('incident') != -1 and (lowered_message.find('create') != -1
                                                   or lowered_message.find('open') != -1
                                                   or lowered_message.find('new') != -1):
        if user_email:
            demisto_user = demisto.findUser(email=user_email)
        else:
            demisto_user = demisto.findUser(username=username)

        if not demisto_user and not allow_external_incidents_creation:
            data = 'You are not allowed to create incidents.'
        else:
            data = process_incident_create_message(demisto_user, message)
            formatted_message = urlify_hyperlinks(data)
    else:
        try:
            return_card = True
            data = demisto.directMessage(message, username, user_email, allow_external_incidents_creation)
            if data.startswith('`'):  # We got a list of incidents/tasks:
                demisto.debug(data)
                data_by_line: list = data.replace('```', '').strip().split('\n')
                demisto.debug(data_by_line)
                return_card = True
                if data_by_line[0].startswith('Task'):
                    attachment = process_tasks_list(data_by_line)
                else:
                    attachment = process_incidents_list(data_by_line)
            else:  # Unknown direct message
                attachment = process_unknown_message(data)
        except Exception as e:
            data = str(e)
    if return_card:
        conversation = {
            'type': 'message',
            'attachments': [attachment]
        }
    else:
        formatted_message = formatted_message or data
        conversation = {
            'type': 'message',
            'text': formatted_message
        }
    send_message_request(conversation_id, conversation, integration_context)


def entitlement_handler(integration_context: dict, request_body: dict, value: dict, conversation_id: str):
    response: str = value.get('response', '')
    entitlement_guid: str = value.get('entitlement', '')
    investigation_id: str = value.get('investigation_id', '')
    task_id: str = value.get('task_id', '')
    from_property: dict = request_body.get('from', {})
    team_members_id: str = from_property.get('id', '')
    team_member: dict = get_team_member(integration_context, team_members_id)
    demisto.handleEntitlementForUser(
        incidentID=investigation_id,
        guid=entitlement_guid,
        taskID=task_id,
        email=team_member.get('user_email', ''),
        content=response
    )
    activity_id: str = request_body.get('replyToId', '')
    service_url: str = integration_context.get('service_url', '')
    if not service_url:
        raise ValueError('Did not find service URL. Try messaging the bot on Microsoft Teams')
    update_message(service_url, conversation_id, activity_id, 'Your response was submitted successfully')


def message_handler(integration_context: dict, request_body: dict, channel_data: dict, message: str):
    channel: dict = channel_data.get('channel', {})
    channel_id: str = channel.get('id', '')
    team_id: str = channel_data.get('team', {}).get('id', '')

    from_property: dict = request_body.get('from', {})
    team_member_id: str = from_property.get('id', '')

    if integration_context.get('teams'):
        teams: list = json.loads(integration_context['teams'])
        for team in teams:
            if team.get('team_id', '') == team_id:
                mirrored_channels: list = team.get('mirrored_channels', [])
                for mirrored_channel in mirrored_channels:
                    if mirrored_channel.get('channel_id') == channel_id:
                        if mirrored_channel.get('mirror_direction', '') != 'FromDemisto' \
                                and 'none' not in mirrored_channel.get('mirror_type', ''):
                            investigation_id: str = mirrored_channel.get('investigation_id', '')
                            username: str = from_property.get('name', '')
                            user_email: str = get_team_member(integration_context, team_member_id).get('user_mail', '')

                            demisto.addEntry(
                                id=investigation_id,
                                entry=message,
                                username=username,
                                email=user_email,
                                footer=f'\n**{ENTRY_FOOTER}**'
                            )
                        return


@APP.route('/', methods=['POST'])
def messages() -> Response:
    """Main bot handler"""
    request_body: dict = request.json
    integration_context: dict = demisto.getIntegrationContext()
    service_url: str = request_body.get('serviceUrl', '')
    if service_url:
        service_url = service_url[:-1] if (service_url and service_url.endswith('/')) else service_url
        integration_context['service_url'] = service_url
        demisto.setIntegrationContext(integration_context)

    channel_data: dict = request_body.get('channelData', {})
    event_type: str = channel_data.get('eventType', '')

    conversation: dict = request_body.get('conversation', {})
    conversation_type: str = conversation.get('conversationType', '')
    conversation_id: str = conversation.get('id', '')

    message_text: str = request_body.get('text', '')

    # Remove bot mention
    bot_name = integration_context.get('bot_name', '')
    formatted_message: str = message_text.replace(f'<at>{bot_name}</at>', '')

    value: dict = request_body.get('value', {})

    if event_type == 'teamMemberAdded':
        member_added_handler(request_body, service_url, channel_data)
    elif value:
        # In TeamsAsk process
        entitlement_handler(integration_context, request_body, value, conversation_id)
    elif conversation_type == 'personal':
        direct_message_handler(integration_context, request_body, conversation, formatted_message)
    else:
        message_handler(integration_context, request_body, channel_data, formatted_message)

    demisto.updateModuleHealth('')
    return Response(status=200)


def long_running_loop():
    try:
        port_mapping: str = PARAMS.get('longRunningPort', '')
        if port_mapping:
            port: int = int(port_mapping.split(':')[0])
        else:
            raise ValueError('No port mapping was provided')
        Thread(target=channel_mirror_loop, daemon=True).start()

        http_server = WSGIServer(('', port), APP)
        http_server.serve_forever()
    except Exception as e:
        raise ValueError(str(e))


def test_module():
    """
    Tests token retrieval for Bot Framework API
    """
    get_bot_access_token()
    demisto.results('ok')


def test_command():
    print(demisto.getIntegrationContext())


def main():
    """ COMMANDS MANAGER / SWITCH PANEL """

    commands = {
        'test-module': test_module,
        'long-running-execution': long_running_loop,
        'send-notification': send_message,
        'mirror-investigation': mirror_investigation,
        'close-channel': close_channel,
        'microsoft-teams-create-team': create_team,
        # 'microsoft-teams-send-file': send_file,
        'test-command': test_command
    }

    ''' EXECUTION '''
    try:
        handle_proxy()
        command: str = demisto.command()
        LOG(f'Command being called is {command}')
        if command in commands.keys():
            commands[command]()
    # Log exceptions
    except Exception as e:
        if command == 'long-running-execution':
            LOG(str(e))
            LOG.print_log()
            demisto.updateModuleHealth(str(e))
        else:
            return_error(str(e))


if __name__ == "builtins":
    main()
