import argparse
import requests
import base64
import urllib.parse
import json
import time, datetime
import sys
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
import configparser
config = configparser.ConfigParser()
import random
import threading

# Optional tvg-id based group-title overrides for cases where
# SiriusXM's browse API returns an incorrect decorations.genre value.
# Add more entries as "tvg-id": "Group Name" if needed.
CHANNEL_GROUP_OVERRIDES = {
    "1308": "Workout",  # Alt Workout
    "1302": "Party",  # Oldies Party
    "1085": "The 70s Decade",  # 70s on 7 Dance/R&B
    "1177": "The 70s Decade",  # 70s on 7 Just Music
    "739": "Country",  # Savior Sunday Daily by Carrie's Country
}

# Optional UUID-based x-sxm-type overrides. Use this only if both the
# authenticated browse API and public /channels page disagree with reality.
# Add entries as "channel-uuid": "channel-xtra" or "channel-linear".
CHANNEL_TYPE_OVERRIDES = {
}

class SiriusXM:
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36'
    REST_FORMAT = 'https://api.edge-gateway.siriusxm.com/{}'
    CDN_URL = "https://imgsrv-sxm-prod-device.streaming.siriusxm.com/{}"

    def __init__(self, username, password):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': self.USER_AGENT,
            'Accept': 'application/json',
            'Origin': 'https://www.siriusxm.com',
            'Referer': 'https://www.siriusxm.com/',
        })
        self.username = username
        self.password = password
        self.playlists = {}
        self.channels = None
        self.channel_ref = None
        self.public_channels = None
        self.m3u8dat = None
        self.stream_urls = {}
        self.xtra_streams = {}
        self.xtra_metadata = {}
        self.xtra_state = {}
        self.xtra_playlists = {}
        self.xtra_session_metadata = {}
        # Number of XTRA tracks to stitch into one VOD playlist. A finite
        # stitched playlist lets normal HLS players start at segment 0 and
        # continue across several XTRA tracks without needing SXM credentials.
        self.xtra_stitch_tracks = int(os.environ.get('XTRA_STITCH_TRACKS', '12'))
        self.xtra_extend_threshold = float(os.environ.get('XTRA_EXTEND_THRESHOLD', '0.70'))
        self.xtra_playlist_max_age = int(os.environ.get('XTRA_PLAYLIST_MAX_AGE', '21600'))
        self.prevcount = 0
        threading.Thread(target=self.cleanup_streaminfo, daemon=True).start()
    
    @staticmethod
    def log(x):
        print('{} <SiriusXM>: {}'.format(datetime.datetime.now().strftime('%d.%b %Y %H:%M:%S'), x))


    #TODO: Figure out if authentication is a valid method anymore. It might need a new login each time.
    def is_logged_in(self):
        return 'Authorization' in self.session.headers

    def is_session_authenticated(self):
        return 'Authorization' in self.session.headers
    
    def sfetch(self, url, retries=0):
        # Fetch stream/key/segment data. If SXM/CDN returns a client-side error
        # such as 401/403/404, try a fresh login once or twice, then fail
        # cleanly instead of crashing the server.
        if retries >= 2:
            self.log("Failed to reauthenticate after stream fetch error.")
            return None

        try:
            res = self.session.get(url, timeout=15)
        except requests.RequestException as e:
            self.log("Stream fetch request failed: {}".format(e))
            return None

        if res.status_code != 200:
            if 400 <= res.status_code < 500:
                self.log("Stream fetch returned {}. Reauthenticating and retrying.".format(res.status_code))
                self.login()
                self.authenticate()
                return self.sfetch(url, retries=retries+1)

            self.log("Failed to receive stream data. Error code {}".format(str(res.status_code)))
            return None

        return res.content

    def get(self, method, params={}, authenticate=True, retries=0):
        if retries >= 3:
            self.log("Max retries hit on {} using method Get".format(method))
            return None
        if authenticate and not self.is_session_authenticated() and not self.authenticate():
            self.log('Unable to authenticate')
            return None

        res = self.session.get(self.REST_FORMAT.format(method), params=params)
        if res.status_code != 200:
            if res.status_code >= 400 and res.status_code < 500:
                self.login()
                self.authenticate()
                return self.post(method, postdata=params, authenticate=authenticate, retries=retries+1)
            self.log('Received status code {} for method \'{}\''.format(res.status_code, method))
            return None

        try:
            return res.json()
        except ValueError:
            self.log('Error decoding json for method \'{}\''.format(method))
            return None

    def post(self, method, postdata, authenticate=True, headers={}, retries=0):
        if retries >= 3:
            self.log("Max retries hit on {} using method Post".format(method))
            return None

        if authenticate and not self.is_session_authenticated() and not self.authenticate():
            self.log('Unable to authenticate')
            return None

        try:
            res = self.session.post(
                self.REST_FORMAT.format(method),
                data=json.dumps(postdata),
                headers=headers,
                timeout=15
            )
        except requests.RequestException as e:
            self.log("POST request failed for {}: {}".format(method, e))
            return None

        if res.status_code != 200 and res.status_code != 201:
            # Only retry authentication for calls that actually require auth.
            # login() uses post(..., authenticate=False), so retrying login there
            # would recurse forever if SXM returns a 4xx during device setup.
            if 400 <= res.status_code < 500 and authenticate:
                self.log("POST {} returned {}. Reauthenticating.".format(method, res.status_code))
                self.login()
                self.authenticate()
                return self.post(method, postdata, authenticate, headers, retries + 1)

            self.log('Received status code {} for method \'{}\''.format(res.status_code, method))
            return None

        try:
            resjson = res.json()
        except ValueError:
            self.log('Error decoding json for method \'{}\''.format(method))
            return None

        bearer_token = resjson.get("grant") or resjson.get("accessToken")
        if bearer_token:
            self.session.headers.update({"Authorization": f"Bearer {bearer_token}"})

        return resjson

    def login(self):
        # Four layer process
        # Assuming the login can work separate from Auth, this is split into two connections:
        # 1) device acknowledge
        # 2) grant anonymous permission
        # The following is reserved for Authentication:
        # Login
        # Affirm Authentication

        # do a completely new session
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': self.USER_AGENT,
            'Accept': 'application/json',
            'Origin': 'https://www.siriusxm.com',
            'Referer': 'https://www.siriusxm.com/',
        })

        postdata = {
            'devicePlatform': "web-desktop",
            'deviceAttributes': {
                'browser': {
                    'browserVersion': "7.74.0",
                    'userAgent': self.USER_AGENT,
                    'sdk': 'web',
                    'app': 'web',
                    'sdkVersion': "7.74.0",
                    'appVersion': "7.74.0"
                }
            },
            'grantVersion': 'v2'
        }
        sxmheaders = {
            "x-sxm-tenant": "sxm",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://www.siriusxm.com",
            "Referer": "https://www.siriusxm.com/",
        }
        data = self.post('device/v1/devices', postdata, authenticate=False,headers=sxmheaders)
        if not data:
            self.log("Error creating device session: {}".format(data))
            return False

        # Once device is registered, grant anonymous permissions 
        data = self.post('session/v1/sessions/anonymous', {}, authenticate=False,headers=sxmheaders)
        if not data:
            self.log("Error validating anonymous session: {}".format(data))
            return False
        try:
            return "accessToken" in data and self.is_logged_in()
        except KeyError:
            self.log('Error decoding json response for login')
            return False
        


    def authenticate(self):
        if not self.is_logged_in() and not self.login():
            self.log('Unable to authenticate because login failed')
            return False

        postdata = {
            "handle": self.username,
            "password": self.password
        }
        data = self.post('identity/v1/identities/authenticate/password', postdata, authenticate=False)
        if not data:
            return False

        
        autheddata = self.post('session/v1/sessions/authenticated', {}, authenticate=False)

        try:
            return autheddata['sessionType'] == "authenticated" and self.is_session_authenticated()
        except KeyError:
            self.log('Error parsing json response for authentication')
            return False


    def _json_object_around(self, text, pos):
        # Return a JSON object string surrounding pos, if one can be found.
        # Used for parsing embedded public channel objects from siriusxm.com/channels.
        start = text.rfind('{', 0, pos)
        while start != -1:
            depth = 0
            in_string = False
            escape = False
            for i in range(start, len(text)):
                ch = text[i]
                if in_string:
                    if escape:
                        escape = False
                    elif ch == '\\':
                        escape = True
                    elif ch == '"':
                        in_string = False
                else:
                    if ch == '"':
                        in_string = True
                    elif ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            candidate = text[start:i + 1]
                            try:
                                obj = json.loads(candidate)
                                if isinstance(obj, dict) and "streamingChannelNumber" in obj and "uuid" in obj:
                                    return candidate
                            except Exception:
                                break
                            break
            start = text.rfind('{', 0, start)
        return None

    def _normalize_public_text(self, value):
        # Clean text pulled from siriusxm.com/channels before writing it into M3U.
        # This repairs common mojibake from UTF-8 text decoded as Latin-1.
        if value is None:
            return ""
        value = str(value)
        try:
            import html as html_lib
            import unicodedata
            value = html_lib.unescape(value)
            if any(bad in value for bad in ("\u00c3", "\u00c2", "\u00e2")):
                try:
                    repaired = value.encode("latin-1").decode("utf-8")
                    if repaired:
                        value = repaired
                except Exception:
                    pass
            value = unicodedata.normalize("NFC", value)
        except Exception:
            pass
        return value.strip()

    def fetch_public_channels(self):
        # Optional public-page metadata source used only for M3U generation.
        # Playback/tuneSource/peek still use the authenticated API.
        if self.public_channels is not None:
            return self.public_channels

        results = {}
        try:
            import html as html_lib
            import re

            res = requests.get(
                "https://www.siriusxm.com/channels",
                headers={
                    "User-Agent": self.USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                timeout=15
            )
            if res.status_code != 200:
                self.log("Public channel page returned status {}".format(res.status_code))
                self.public_channels = {}
                return self.public_channels

            raw_text = res.content.decode("utf-8", errors="replace")
            decoded_variants = []
            decoded_variants.append(raw_text)
            decoded_variants.append(html_lib.unescape(raw_text))

            # The channel objects are usually embedded as escaped JSON inside HTML.
            # unicode_escape turns strings like \"displayName\" into "displayName".
            for candidate in list(decoded_variants):
                try:
                    decoded_variants.append(bytes(candidate, "utf-8").decode("unicode_escape"))
                except Exception:
                    pass

            seen_objects = set()
            for source in decoded_variants:
                for match in re.finditer(r'"streamingChannelNumber"\s*:\s*\d+', source):
                    obj_text = self._json_object_around(source, match.start())
                    if not obj_text or obj_text in seen_objects:
                        continue
                    seen_objects.add(obj_text)
                    try:
                        item = json.loads(obj_text)
                    except Exception:
                        continue

                    channel_number = item.get("streamingChannelNumber") or item.get("xmChannelNumber")
                    channel_uuid = item.get("uuid")
                    if not channel_number or not channel_uuid:
                        continue

                    title = self._normalize_public_text(item.get("displayName") or item.get("name"))
                    genre = self._normalize_public_text(item.get("genreTitle") or item.get("genre"))
                    is_xtra = bool(item.get("xtra_channel"))

                    logo_path = ""
                    web_image = item.get("web_2_0_image")
                    if isinstance(web_image, dict):
                        logo_path = web_image.get("url") or ""
                    logo_path = self._normalize_public_text(logo_path or item.get("colorLogo") or item.get("greyscaleLogo") or "")

                    results[str(channel_number)] = {
                        "title": title,
                        "genre": genre,
                        "channel_type": "channel-xtra" if is_xtra else "channel-linear",
                        "id": channel_uuid,
                        "logo_path": logo_path,
                    }

            self.public_channels = results
            self.log("Loaded {} public channel M3U overrides".format(len(results)))
            return self.public_channels

        except Exception as e:
            self.log("Failed to fetch public channel metadata: {}".format(e))
            self.public_channels = {}
            return self.public_channels


    def _authenticated_type_by_uuid(self):
        # Build a UUID -> authenticated API channel_type map. The public
        # /channels page has better names/groups, but the authenticated browse
        # API has proven more reliable for whether a UUID is channel-linear
        # or channel-xtra.
        lookup = {}
        if not self.channels:
            self.get_channels()
        for channel in self.channels or []:
            channel_uuid = channel.get("id")
            channel_type = channel.get("channel_type")
            if channel_uuid and channel_type:
                lookup[str(channel_uuid)] = channel_type
        return lookup

    def _resolve_m3u_channel_type(self, original_channel, public_override, auth_type_by_uuid):
        # Priority:
        # 1) manual UUID override
        # 2) authenticated API type for the final UUID
        # 3) authenticated API type for the original UUID
        # 4) public /channels xtra_channel-derived type
        # 5) original/fallback linear type
        public_uuid = public_override.get("id") if isinstance(public_override, dict) else None
        original_uuid = original_channel.get("id")

        for channel_uuid in (public_uuid, original_uuid):
            if channel_uuid and str(channel_uuid) in CHANNEL_TYPE_OVERRIDES:
                return CHANNEL_TYPE_OVERRIDES[str(channel_uuid)]

        if public_uuid and str(public_uuid) in auth_type_by_uuid:
            return auth_type_by_uuid[str(public_uuid)]

        if original_uuid and str(original_uuid) in auth_type_by_uuid:
            return auth_type_by_uuid[str(original_uuid)]

        if isinstance(public_override, dict) and public_override.get("channel_type"):
            return public_override.get("channel_type")

        return original_channel.get("channel_type", "channel-linear")


    def get_playlist(self):
        if not self.channels:
            self.get_channels()
        if not self.m3u8dat:
            public_map = self.fetch_public_channels()
            auth_type_by_uuid = self._authenticated_type_by_uuid()
            data = []
            data.append("#EXTM3U")
            m3umetadata = """#EXTINF:-1 tvg-id="{}" tvg-chno="{}" tvg-logo="{}" group-title="{}" x-sxm-type="{}",{}\n{}"""
            for num, channel in enumerate(self.channels, start=1):
                channel_id = channel["channel_id"]
                override = public_map.get(str(channel_id), {})

                title = override.get("title") or channel["title"]
                genre = override.get("genre") or channel["genre"] or "Other"
                logo = channel["logo"]
                channel_uuid = override.get("id") or channel["id"]
                channel_type = self._resolve_m3u_channel_type(channel, override, auth_type_by_uuid)

                # SiriusXM's authenticated browse API occasionally returns an
                # incorrect decorations.genre. The public channels page is used
                # first when available; this manual map remains as a final fallback.
                genre = CHANNEL_GROUP_OVERRIDES.get(str(channel_id), genre)

                if channel_type == "channel-xtra":
                    if genre.strip().lower() == "all xtra":
                        group_title = "All XTRA"
                    else:
                        group_title = "{} XTRA".format(genre)
                else:
                    group_title = genre

                url = "/listen/{}".format(channel_uuid)
                formattedm3udata = m3umetadata.format(channel_id, num, logo, group_title, channel_type, title, url)
                data.append(formattedm3udata)
            self.m3u8dat = "\n".join(data)

        return self.m3u8dat

    def get_channels(self):
        # download channel list if necessary
        # todo: find out if the container ID or the UUID changes; how to auto fetch if so.
        # channel list is split up. gotta get every channel

        if not self.channels:
            self.channels = []
            # todo: this is how the web traffic processed the channels, might not be needed though
            initData = {
                "containerConfiguration": {
                    "3JoBfOCIwo6FmTpzM1S2H7": {
                        "filter": {
                            "one": {
                                "filterId": "all"
                            }
                        },
                        "sets": {
                            "5mqCLZ21qAwnufKT8puUiM": {
                                "sort": {
                                    "sortId": "CHANNEL_NUMBER_ASC"
                                }
                            }
                        }
                    }
                },
                "pagination": {
                    "offset": {
                        "containerLimit": 3,
                        "setItemsLimit": 50
                    }
                },
                "deviceCapabilities": {
                    "supportsDownloads": False
                }
            }
            data = self.post('browse/v1/pages/curated-grouping/403ab6a5-d3c9-4c2a-a722-a94a6a5fd056/view', initData)
            if not data:
                self.log('Unable to get init channel list')
                return (None, None)
            for channel in data["page"]["containers"][0]["sets"][0]["items"]:
                title = channel["entity"]["texts"]["title"]["default"]
                description = channel["entity"]["texts"]["description"]["default"]
                genre = channel["decorations"]["genre"] if "genre" in channel["decorations"] else ""
                channel_id = channel["decorations"]["channelNumber"]
                channel_type = channel["actions"]["play"][0]["entity"]["type"]
                logo = channel["entity"]["images"]["tile"]["aspect_1x1"]["preferred"]["url"]
                logo_width = channel["entity"]["images"]["tile"]["aspect_1x1"]["preferred"]["width"]
                logo_height = channel["entity"]["images"]["tile"]["aspect_1x1"]["preferred"]["height"]
                id = channel["entity"]["id"]
                jsonlogo = json.dumps({
                    "key": logo,
                    "edits":[
                        {"format":{"type":"jpeg"}},
                        {"resize":{"width":logo_width,"height":logo_height}}
                    ]
                },separators=(',', ':'))
                b64logo = base64.b64encode(jsonlogo.encode("ascii")).decode("utf-8")
                self.channels.append({
                    "title": title,
                    "description": description,
                    "genre": genre,
                    "channel_id": channel_id,
                    "channel_type": channel_type,
                    "logo":  self.CDN_URL.format(b64logo),
                    "url": "/listen/{}".format(id),
                    "id": id
                })
                
            channellen = data["page"]["containers"][0]["sets"][0]["pagination"]["offset"]["size"]
            for offset in range(50,channellen,50):
                postdata = {
                    "filter": {
                        "one": {
                        "filterId": "all"
                        }
                    },
                    "sets": {
                        "5mqCLZ21qAwnufKT8puUiM": {
                        "sort": {
                            "sortId": "CHANNEL_NUMBER_ASC"
                        },
                        "pagination": {
                            "offset": {
                            "setItemsOffset": offset,
                            "setItemsLimit": 50
                            }
                        }
                        }
                    },
                    "pagination": {
                        "offset": {
                        "setItemsLimit": 50
                        }
                    }
                }
                data = self.post('browse/v1/pages/curated-grouping/403ab6a5-d3c9-4c2a-a722-a94a6a5fd056/containers/3JoBfOCIwo6FmTpzM1S2H7/view', postdata, initData)
                if not data:
                    self.log('Unable to get fetch channel list chunk')
                    return (None, None)
                for channel in data["container"]["sets"][0]["items"]:
                    title = channel["entity"]["texts"]["title"]["default"]
                    description = channel["entity"]["texts"]["description"]["default"]
                    genre = channel["decorations"]["genre"] if "genre" in channel["decorations"] else ""
                    channel_id = channel["decorations"]["channelNumber"]
                    channel_type = channel["actions"]["play"][0]["entity"]["type"]
                    logo = channel["entity"]["images"]["tile"]["aspect_1x1"]["preferred"]["url"]
                    logo_width = channel["entity"]["images"]["tile"]["aspect_1x1"]["preferred"]["width"]
                    logo_height = channel["entity"]["images"]["tile"]["aspect_1x1"]["preferred"]["height"]
                    id = channel["entity"]["id"]
                    jsonlogo = json.dumps({
                        "key": logo,
                        "edits":[
                            {"format":{"type":"jpeg"}},
                            {"resize":{"width":logo_width,"height":logo_height}}
                        ]
                    },separators=(',', ':'))
                    b64logo = base64.b64encode(jsonlogo.encode("ascii")).decode("utf-8")
                    self.channels.append({
                        "title": title,
                        "description": description,
                        "genre": genre,
                        "channel_id": channel_id,
                        "channel_type": channel_type,
                        "logo":  self.CDN_URL.format(b64logo),
                        "url": "/listen/{}".format(id),
                        "id": id
                    })

        return self.channels

    #temporary patch, should do a reverse index lookup table
    def get_channel_info(self,id):
        if not self.channels:
            self.get_channels()
        for ch in self.channels:
            if id == ch["id"]:
                return ch
        return None

    def get_tuner(self, id, force_next=False):
        channel_info = self.get_channel_info(id)
        channel_type = channel_info["channel_type"] if channel_info and "channel_type" in channel_info else "channel-linear"
        isXtra = channel_type == "channel-xtra"

        # Linear channels can reuse cached tune info. XTRA channels can also
        # reuse cached tune info unless a real next-track peek is requested.
        if id in self.stream_urls and (not isXtra or not force_next):
            return self.stream_urls[id]

        postdata = {
            "id": id,
            "type": channel_type,
            "hlsVersion": "V3",
            "mtcVersion": "V2"
        }
        if isXtra:
            # False avoids SXM resuming XTRA tracks near the end when possible.
            postdata["trackResumeSupported"] = False

        # Keep XTRA continuity state separate from the stream URL cache. If we
        # lose this state, tuneSource may resume the same track near its end.
        state = self.xtra_state.get(id, {}) if isXtra else {}
        cached_stream = self.stream_urls.get(id, {}) if isXtra else {}
        contextId = state.get("sourceContextId") or cached_stream.get("sourceContextId")
        sequenceToken = state.get("sequenceToken") or cached_stream.get("sequenceToken") or self.xtra_metadata.get(id, {}).get("sequenceToken")

        use_peek = bool(isXtra and contextId and force_next)
        if use_peek:
            postdata["sourceContextId"] = contextId
            if sequenceToken:
                postdata["sequenceToken"] = sequenceToken
            self.log("XTRA peek requested for {} (has sequenceToken: {})".format(id, bool(sequenceToken)))
        else:
            postdata["manifestVariant"] = "WEB" if channel_type == "channel-linear" else "FULL"
            if isXtra:
                self.log("XTRA tuneSource requested for {}".format(id))

        tunerUrl = 'playback/play/v1/peek' if use_peek else 'playback/play/v1/tuneSource'
        data = self.post(tunerUrl, postdata, authenticate=True)
        if not data:
            self.log("Couldn't tune channel.")
            return False

        if isXtra and not force_next:
            # Only publish metadata for an initial tuneSource.
            # Background peek calls are used to build future stitched queues;
            # publishing their metadata here makes future songs appear early.
            self.update_xtra_metadata(id, data)

        try:
            primarystreamurl = data["streams"][0]["urls"][0]["url"]
        except (KeyError, IndexError, TypeError):
            self.log("Unable to parse stream URL from tune response")
            return False

        streaminfo = {}
        sessionId = None
        sourceContextId = None
        sequenceToken = None

        if isXtra:
            sessionId = str(random.randint((10**37), (10**38)))
            sourceContextId = self._first_present(data, ["sourceContextId"]) or contextId
            sequenceToken = self._first_present(data, ["sequenceToken"]) or sequenceToken
            streaminfo["sessionId"] = sessionId
            streaminfo["expires"] = time.time() + max(
                1800,
                int(getattr(self, "xtra_playlist_max_age", 600)) + 900
            )

        base_url, m3u8_loc = primarystreamurl.rsplit('/', 1)
        streaminfo["base_url"] = base_url
        streaminfo["sources"] = m3u8_loc
        streaminfo["chid"] = base_url.split('/')[-2]
        streaminfo["sourceContextId"] = sourceContextId
        if sequenceToken:
            streaminfo["sequenceToken"] = sequenceToken

        streamdata = self.sfetch(primarystreamurl)
        if not streamdata:
            self.log("Failed to fetch m3u8 stream details")
            return False
        streamdata = streamdata.decode("utf-8")

        for line in streamdata.splitlines():
            if line.find("256k") > 0 and line.endswith("m3u8"):
                streaminfo["quality"] = line
                streaminfo["HLS"] = line.split("/")[0]
                break

        if "quality" not in streaminfo or "HLS" not in streaminfo:
            self.log("Unable to find 256k HLS playlist in stream details")
            return False

        if isXtra:
            track_metadata = self._extract_xtra_track_metadata(id, data)
            if sourceContextId:
                track_metadata["sourceContextId"] = sourceContextId
            if sequenceToken:
                track_metadata["sequenceToken"] = sequenceToken
            track_metadata["sessionId"] = sessionId
            streaminfo["trackMetadata"] = track_metadata
            self.xtra_session_metadata[sessionId] = track_metadata

            self.xtra_streams[sessionId] = streaminfo
            if sourceContextId or sequenceToken:
                self.xtra_state[id] = {
                    "sourceContextId": sourceContextId,
                    "sequenceToken": sequenceToken,
                    "updatedAt": time.time(),
                }
            self.log("XTRA {} returned stream {} (has sequenceToken: {})".format(
                "peek" if use_peek else "tuneSource",
                streaminfo.get("chid", "unknown"),
                bool(sequenceToken)
            ))
            # New tune/peek means cached local XTRA playlist is no longer valid.
            self.xtra_playlists.pop(id, None)

        self.stream_urls[id] = streaminfo
        return streaminfo

    def _parse_hls_duration(self, playlist_text):
        duration = 0.0
        for line in playlist_text.splitlines():
            if line.startswith("#EXTINF:"):
                try:
                    duration += float(line.split(":", 1)[1].split(",", 1)[0])
                except (ValueError, IndexError):
                    pass
        return duration

    def _segment_number(self, segment_name):
        # SXM segment names usually end with _00000012_v3.aac. Return 12.
        clean = segment_name.split('?', 1)[0].rsplit('/', 1)[-1]
        parts = clean.split('_')
        if len(parts) >= 2:
            try:
                return int(parts[-2])
            except (TypeError, ValueError):
                return None
        return None

    def _playlist_segment_summary(self, playlist_text):
        segments = [line.rstrip() for line in playlist_text.splitlines() if line.rstrip().endswith('.aac')]
        numbers = [self._segment_number(seg) for seg in segments]
        numbers = [n for n in numbers if n is not None]
        return {
            "segments": segments,
            "count": len(segments),
            "first": numbers[0] if numbers else None,
            "last": numbers[-1] if numbers else None,
        }

    def _extract_key_line(self, playlist_text):
        for line in playlist_text.splitlines():
            if line.startswith("#EXT-X-KEY"):
                return line.replace("https://api.edge-gateway.siriusxm.com/playback/key/v1/", "/key/", 1)
        return None

    def _extract_target_duration(self, playlist_text):
        for line in playlist_text.splitlines():
            if line.startswith("#EXT-X-TARGETDURATION"):
                return line
        return "#EXT-X-TARGETDURATION:10"

    def _extract_version(self, playlist_text):
        for line in playlist_text.splitlines():
            if line.startswith("#EXT-X-VERSION"):
                return line
        return "#EXT-X-VERSION:3"

    def _extract_media_sequence(self, playlist_text):
        for line in playlist_text.splitlines():
            if line.startswith("#EXT-X-MEDIA-SEQUENCE"):
                return line
        return "#EXT-X-MEDIA-SEQUENCE:0"

    def _extract_segments_with_durations(self, playlist_text):
        # Return [(duration_line, segment_line), ...] for AAC segments.
        pairs = []
        pending_duration = None
        for line in playlist_text.splitlines():
            stripped = line.rstrip()
            if stripped.startswith("#EXTINF:"):
                pending_duration = stripped
            elif stripped.endswith(".aac"):
                pairs.append((pending_duration or "#EXTINF:10.0,", stripped))
                pending_duration = None
        return pairs

    def _rewrite_media_playlist(self, channel_id, playlist_text, sessionId='', is_xtra=False):
        playlist_text = playlist_text.replace(
            "https://api.edge-gateway.siriusxm.com/playback/key/v1/",
            "/key/",
            1
        )

        lines = []
        for line in playlist_text.splitlines():
            if line.rstrip().endswith('.aac'):
                line = '{}/{}?{}'.format(channel_id, line, sessionId)
            lines.append(line)

        return '\n'.join(lines).encode('utf-8')

    def _build_xtra_stitched_playlist(self, channel_id, tracks):
        # tracks: list of dicts with playlist_text, sessionId. Build a finite VOD
        # playlist that starts at segment 0 of the current track and continues
        # into the prefetched next track, so clients do not stop at the boundary.
        if not tracks:
            return None

        first_text = tracks[0]["playlist_text"]
        lines = [
            "#EXTM3U",
            self._extract_version(first_text),
            self._extract_target_duration(first_text),
            self._extract_media_sequence(first_text),
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-START:TIME-OFFSET=0,PRECISE=YES",
            "#EXT-X-DISCONTINUITY-SEQUENCE:0",
        ]

        for index, track in enumerate(tracks):
            playlist_text = track["playlist_text"]
            sessionId = track.get("sessionId", "")
            key_line = self._extract_key_line(playlist_text)
            if index > 0:
                lines.append("#EXT-X-DISCONTINUITY")
            if key_line:
                lines.append(key_line)
            for duration_line, segment_line in self._extract_segments_with_durations(playlist_text):
                lines.append(duration_line)
                lines.append('{}/{}?{}'.format(channel_id, segment_line, sessionId))

        lines.append("#EXT-X-ENDLIST")
        return '\n'.join(lines).encode('utf-8')

    def _fetch_xtra_playlist_for_streaminfo(self, channel_id, streaminfo):
        sessionId = streaminfo.get("sessionId", "")
        aacurl = "{}/{}".format(streaminfo["base_url"], streaminfo["quality"])
        data = self.sfetch(aacurl)
        if not data:
            return None
        playlist_text = data.decode("utf-8")
        summary = self._playlist_segment_summary(playlist_text)
        self.log("XTRA playlist for {}: segments={} first={} last={} session={}".format(
            channel_id, summary["count"], summary["first"], summary["last"], sessionId
        ))
        return {
            "playlist_text": playlist_text,
            "sessionId": sessionId,
            "summary": summary,
            "streaminfo": streaminfo,
        }

    def next_xtra_track(self, channel_id):
        channel_info = self.get_channel_info(channel_id)
        if not channel_info or channel_info.get("channel_type") != "channel-xtra":
            return None

        self.log("XTRA manual next requested for {}".format(channel_id))

        # Clear the local stitched queue first so the next /listen request cannot
        # reuse buffered tracks from the previous queue. Then force one SXM peek
        # so the next queue starts with the next XTRA item.
        self.xtra_playlists.pop(channel_id, None)

        streaminfo = self.get_tuner(channel_id, force_next=True)
        if not streaminfo:
            self.log("XTRA manual next failed for {}".format(channel_id))
            return None

        # Prebuild/cache the new queue immediately. This lets the app call
        # /xtra/<id>/next and then reload /listen/<id> without racing the server.
        queued = self._build_and_cache_xtra_queue(channel_id, streaminfo, publish_current_metadata=True)
        if not queued:
            self.log("XTRA manual next could not build a new queue for {}".format(channel_id))
            return None

        meta = self.get_metadata(channel_id, include_queue=False) or {}
        response = dict(meta)
        response.update({
            "ok": True,
            "action": "reload",
            "direction": "next",
            "listenUrl": "/listen/{}".format(channel_id),
            "metadataUrl": "/metadata/{}".format(channel_id),
            "message": "Reload listenUrl and clear/flush the existing HLS buffer."
        })
        return response

    def previous_xtra_track(self, channel_id):
        # The current local HLS stitching design can reliably skip forward by
        # calling SXM peek. Back/previous requires SXM-specific previous-track
        # control semantics that are not implemented here yet.
        channel_info = self.get_channel_info(channel_id)
        if not channel_info or channel_info.get("channel_type") != "channel-xtra":
            return None
        return {
            "ok": False,
            "action": "unsupported",
            "direction": "previous",
            "channelId": channel_id,
            "message": "Previous/back is not implemented server-side yet. Use forward skip first."
        }

    def _first_present(self, data, keys):
        if isinstance(data, dict):
            for key in keys:
                value = data.get(key)
                if value not in (None, ""):
                    return value
            for value in data.values():
                found = self._first_present(value, keys)
                if found not in (None, ""):
                    return found
        elif isinstance(data, list):
            for value in data:
                found = self._first_present(value, keys)
                if found not in (None, ""):
                    return found
        return None

    def _format_sxm_image_url(self, image_key, width=800, height=800):
        if not image_key:
            return ""
        image_key = str(image_key)
        if image_key.startswith("http"):
            if ".m3u8" in image_key or "/audio/" in image_key:
                return ""
            return image_key
        if "/audio/" in image_key or image_key.endswith(".m3u8"):
            return ""
        jsonlogo = json.dumps({
            "key": image_key,
            "edits": [
                {"format": {"type": "jpeg"}},
                {"resize": {"width": width, "height": height}}
            ]
        }, separators=(',', ':'))
        b64logo = base64.b64encode(jsonlogo.encode("ascii")).decode("utf-8")
        return self.CDN_URL.format(b64logo)

    def _xtra_track_item_from_tune(self, tune_data):
        # SXM XTRA tune/peek responses put real song metadata here:
        # streams[].metadata.xtra.items[0] with name, artistName, duration and artwork.
        if not isinstance(tune_data, dict):
            return None
        for stream in tune_data.get("streams", []) or []:
            if not isinstance(stream, dict):
                continue
            items = (((stream.get("metadata") or {}).get("xtra") or {}).get("items") or [])
            for item in items:
                if isinstance(item, dict) and item.get("type") == "xtra-channel-track":
                    return item
            for item in items:
                if isinstance(item, dict) and (item.get("name") or item.get("artistName")):
                    return item
        return None

    def _xtra_art_from_item(self, item):
        if not isinstance(item, dict):
            return ""
        images = item.get("images") or {}
        candidates = [
            (((images.get("tile") or {}).get("aspect_1x1") or {}).get("preferredImage") or {}),
            (((images.get("tile") or {}).get("aspect_1x1") or {}).get("defaultImage") or {}),
            (((images.get("cover") or {}).get("aspect_1x1") or {}).get("preferredImage") or {}),
            (((images.get("cover") or {}).get("aspect_1x1") or {}).get("defaultImage") or {}),
        ]
        for img in candidates:
            if isinstance(img, dict):
                url = img.get("url")
                if url and "/artwork/" in str(url):
                    return self._format_sxm_image_url(url, img.get("width", 800), img.get("height", 800))
        # Fallback: search only within this track item for artwork URLs.
        def walk(obj):
            if isinstance(obj, dict):
                url = obj.get("url")
                if url and "/artwork/" in str(url):
                    return self._format_sxm_image_url(url, obj.get("width", 800), obj.get("height", 800))
                for value in obj.values():
                    found = walk(value)
                    if found:
                        return found
            elif isinstance(obj, list):
                for value in obj:
                    found = walk(value)
                    if found:
                        return found
            return ""
        return walk(item)

    def _extract_xtra_skip_limits(self, tune_data):
        # SXM usually returns XTRA skip availability as:
        # skipLimits.limited.availableForwardSkips / availableBackwardSkips.
        # Keep both flattened fields and the raw skipLimits object so clients
        # can disable/re-enable buttons without hardcoding a fixed skip count.
        def find_skip_limits(obj):
            if isinstance(obj, dict):
                if isinstance(obj.get("skipLimits"), dict):
                    return obj.get("skipLimits")
                for value in obj.values():
                    found = find_skip_limits(value)
                    if found:
                        return found
            elif isinstance(obj, list):
                for value in obj:
                    found = find_skip_limits(value)
                    if found:
                        return found
            return None

        skip_limits = find_skip_limits(tune_data) or {}
        limited = skip_limits.get("limited") if isinstance(skip_limits, dict) else {}
        if not isinstance(limited, dict):
            limited = {}

        def to_int(value, default=0):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        available_forward = to_int(
            limited.get("availableForwardSkips", skip_limits.get("availableForwardSkips") if isinstance(skip_limits, dict) else 0),
            0
        )
        available_backward = to_int(
            limited.get("availableBackwardSkips", skip_limits.get("availableBackwardSkips") if isinstance(skip_limits, dict) else 0),
            0
        )
        more_time = limited.get("moreSkipsAvailableTime") if isinstance(limited, dict) else None
        if more_time is None and isinstance(skip_limits, dict):
            more_time = skip_limits.get("moreSkipsAvailableTime")

        return {
            "availableForwardSkips": available_forward,
            "availableBackwardSkips": available_backward,
            "moreSkipsAvailableTime": more_time,
            "skipLimits": skip_limits,
        }

    def _extract_xtra_track_metadata(self, channel_id, tune_data):
        item = self._xtra_track_item_from_tune(tune_data)
        sequence_token = self._first_present(tune_data, ["sequenceToken"])
        source_context_id = self._first_present(tune_data, ["sourceContextId"])
        if item:
            title = item.get("name") or item.get("title") or ""
            artist = item.get("artistName") or item.get("artist") or ""
            album = item.get("albumName") or item.get("albumTitle") or item.get("album") or ""
            duration_ms = item.get("duration") or item.get("durationMs") or item.get("trackDurationMs") or 0
            image_url = self._xtra_art_from_item(item)
        else:
            title = self._first_present(tune_data, ["trackTitle", "songTitle", "cutTitle", "name", "title"]) or ""
            artist = self._first_present(tune_data, ["artistName", "artist", "artists", "subtitle", "secondaryTitle"]) or ""
            album = self._first_present(tune_data, ["albumName", "albumTitle", "album"]) or ""
            duration_ms = self._first_present(tune_data, ["durationMs", "duration", "trackDurationMs"]) or 0
            image_url = ""

        try:
            duration_ms = int(duration_ms) if duration_ms is not None else 0
        except (TypeError, ValueError):
            duration_ms = 0

        metadata = {
            "channelId": channel_id,
            "title": title or "",
            "artist": artist or "",
            "album": album or "",
            "imageUrl": image_url or "",
            "durationMs": duration_ms,
            "startedAtMs": int(time.time() * 1000),
            "isXtra": True
        }
        if sequence_token:
            metadata["sequenceToken"] = sequence_token
        if source_context_id:
            metadata["sourceContextId"] = source_context_id
        metadata.update(self._extract_xtra_skip_limits(tune_data))
        if item and item.get("id"):
            metadata["trackId"] = item.get("id")
        return metadata

    def update_xtra_metadata(self, channel_id, tune_data):
        try:
            # Look for the correct SXM structure
            streams = tune_data.get("streams", [])
            item = None

            for stream in streams:
                meta = stream.get("metadata", {}).get("xtra", {})
                items = meta.get("items", [])
                if items:
                    item = items[0]
                    break

            if item:
                title = item.get("name", "")
                artist = item.get("artistName", "")
                duration_ms = item.get("duration", 0)

                # Extract album art
                image_url = ""
                try:
                    image_path = item["images"]["tile"]["aspect_1x1"]["preferredImage"]["url"]
                    image_url = self.CDN_URL.format(
                        base64.b64encode(json.dumps({
                            "key": image_path,
                            "edits":[
                                {"format":{"type":"jpeg"}},
                                {"resize":{"width":800,"height":800}}
                            ]
                        }, separators=(',', ':')).encode("ascii")).decode("utf-8")
                    )
                except:
                    image_url = ""

                metadata = {
                    "channelId": channel_id,
                    "title": title,
                    "artist": artist,
                    "album": "",
                    "imageUrl": image_url,
                    "durationMs": int(duration_ms),
                    "startedAtMs": int(time.time() * 1000),
                    "isXtra": True
                }

                # Preserve tokens for playback continuity
                sequence_token = self._first_present(tune_data, ["sequenceToken"])
                source_context_id = self._first_present(tune_data, ["sourceContextId"])

                if sequence_token:
                    metadata["sequenceToken"] = sequence_token
                if source_context_id:
                    metadata["sourceContextId"] = source_context_id
                metadata.update(self._extract_xtra_skip_limits(tune_data))

                self.xtra_metadata[channel_id] = metadata
                return metadata

        except Exception as e:
            self.log(f"Metadata parse error: {e}")

        return self.xtra_metadata.get(channel_id, {})

    def get_metadata(self, channel_id, position_ms=None, include_queue=True):
        channel_info = self.get_channel_info(channel_id)
        is_xtra = channel_info and channel_info.get("channel_type") == "channel-xtra"
        if is_xtra and channel_id not in self.xtra_metadata:
            self.get_tuner(channel_id)

        if is_xtra and position_ms is not None:
            meta = self._metadata_for_position(channel_id, position_ms)
        else:
            meta = self.xtra_metadata.get(channel_id)

        if meta:
            out = dict(meta)
            cached = self.xtra_playlists.get(channel_id) or {}
            if include_queue and cached.get("queue"):
                out["queue"] = cached.get("queue")
                out["queueDurationMs"] = cached.get("durationMs", 0)
                out["queueGeneratedAtMs"] = cached.get("queueGeneratedAtMs", out.get("queueGeneratedAtMs", 0))
            return out

        if channel_info:
            return {
                "channelId": channel_id,
                "title": channel_info.get("title", ""),
                "artist": "",
                "album": "",
                "imageUrl": channel_info.get("logo", ""),
                "durationMs": 0,
                "startedAtMs": 0,
                "isXtra": bool(is_xtra)
            }
        return None

    def get_tuner_cached(self,id,sessionId):
            return self.xtra_streams[sessionId]
    
    def cleanup_streaminfo(self,delay=600):
        while True:
            now = time.time()
            keys_to_delete = [sessionId for sessionId in self.xtra_streams.keys() if self.xtra_streams[sessionId]["expires"] < now]
            for k in keys_to_delete:
                del self.xtra_streams[k]
            time.sleep(delay)
    
    def _prefetch_xtra_tracks(self, channel_id, streaminfo, desired_tracks):
        tracks = []
        current_track = self._fetch_xtra_playlist_for_streaminfo(channel_id, streaminfo)
        if not current_track:
            self.log("Failed to fetch XTRA current playlist for {}".format(channel_id))
            return tracks
        tracks.append(current_track)

        desired_tracks = max(1, int(desired_tracks))
        while len(tracks) < desired_tracks:
            next_streaminfo = self.get_tuner(channel_id, force_next=True)
            if not next_streaminfo:
                self.log("Unable to prefetch XTRA stream {} of {} for {}; serving {} tracks".format(
                    len(tracks) + 1, desired_tracks, channel_id, len(tracks)
                ))
                break
            next_track = self._fetch_xtra_playlist_for_streaminfo(channel_id, next_streaminfo)
            if not next_track:
                self.log("Unable to prefetch XTRA playlist {} of {} for {}; serving {} tracks".format(
                    len(tracks) + 1, desired_tracks, channel_id, len(tracks)
                ))
                break
            tracks.append(next_track)
        return tracks

    def _track_duration_ms_from_playlist(self, playlist_text, meta=None):
        duration = self._parse_hls_duration(playlist_text)
        if duration > 0:
            return int(duration * 1000)
        if meta:
            try:
                d = int(meta.get("durationMs", 0) or 0)
                if d > 0:
                    return d
            except (TypeError, ValueError):
                pass
        return 0

    def _public_metadata(self, meta):
        # Return a copy that is safe/useful for clients.
        if not meta:
            return {}
        return {k: v for k, v in dict(meta).items() if k not in ("sequenceToken", "sourceContextId")}

    def _metadata_for_position(self, channel_id, position_ms):
        cached = self.xtra_playlists.get(channel_id) or {}
        queue = cached.get("queue") or []
        if not queue:
            return self.xtra_metadata.get(channel_id)
        try:
            pos = int(position_ms)
        except (TypeError, ValueError):
            pos = 0
        current = queue[0]
        for item in queue:
            start = int(item.get("startOffsetMs", 0) or 0)
            end = int(item.get("endOffsetMs", start) or start)
            if start <= pos < end:
                current = item
                break
            if pos >= end:
                current = item
        meta = dict(current)
        meta["positionMs"] = pos
        meta["resolvedBy"] = "position"
        # Return position-resolved metadata directly without publishing it as
        # the global current state. The player may ask about a freshly loaded
        # or preloaded queue before it is audible, so global metadata must not
        # be advanced by position lookups.
        return meta

    def _build_and_cache_xtra_queue(self, channel_id, streaminfo, publish_current_metadata=False):
        max_tracks = max(1, int(getattr(self, "xtra_stitch_tracks", 12)))
        tracks = self._prefetch_xtra_tracks(channel_id, streaminfo, max_tracks)
        if not tracks:
            return False

        self.log("Stitched XTRA playlist for {} with {} tracks".format(channel_id, len(tracks)))
        rewritten = self._build_xtra_stitched_playlist(channel_id, tracks)
        if not rewritten:
            return False

        all_segments = []
        total_duration = 0.0
        total_duration_ms = 0
        track_metadata_by_session = {}
        queue = []
        queue_generated_at_ms = int(time.time() * 1000)
        queue_length = len(tracks)
        offset_ms = 0

        for idx, track in enumerate(tracks, start=1):
            playlist_text = track["playlist_text"]
            parsed_duration = self._parse_hls_duration(playlist_text)
            total_duration += parsed_duration
            all_segments.extend(track["summary"]["segments"])
            session_id = track.get("sessionId", "")
            meta = dict((track.get("streaminfo") or {}).get("trackMetadata") or self.xtra_session_metadata.get(session_id, {}) or {})
            duration_ms = self._track_duration_ms_from_playlist(playlist_text, meta)
            if duration_ms <= 0:
                duration_ms = int((parsed_duration or 0) * 1000)
            start_offset_ms = offset_ms
            end_offset_ms = start_offset_ms + max(0, duration_ms)

            meta.update({
                "channelId": channel_id,
                "isXtra": True,
                "sessionId": session_id,
                "trackIndex": idx,
                "queueLength": queue_length,
                "queueGeneratedAtMs": queue_generated_at_ms,
                "startOffsetMs": start_offset_ms,
                "endOffsetMs": end_offset_ms,
                "durationMs": duration_ms or meta.get("durationMs", 0),
            })
            track_metadata_by_session[session_id] = meta
            queue.append(self._public_metadata(meta))
            if session_id:
                self.xtra_session_metadata[session_id] = meta
            offset_ms = end_offset_ms
            total_duration_ms = offset_ms

        if total_duration <= 0:
            total_duration = total_duration_ms / 1000.0 if total_duration_ms else ((self.xtra_metadata.get(channel_id, {}).get("durationMs", 0) or 30000) / 1000.0 * len(tracks))

        self.xtra_playlists[channel_id] = {
            "data": rewritten,
            "started": time.time(),
            "duration": total_duration,
            "durationMs": total_duration_ms,
            "sessionId": tracks[0].get("sessionId", ""),
            "segments": all_segments,
            "last_segment": all_segments[-1] if all_segments else None,
            "served": set(),
            "served_count": 0,
            "served_last": False,
            "stitched_tracks": len(tracks),
            "track_metadata_by_session": track_metadata_by_session,
            "queue": queue,
            "queueGeneratedAtMs": queue_generated_at_ms,
        }
        # Do not always publish queue[0] as the current metadata here.
        # M3You may prefetch/reload /listen near the end of the old queue,
        # while the old audio is still playing. Publishing queue[0] immediately
        # causes the next queue metadata to appear too early.
        #
        # Current metadata is now advanced by:
        #   1) /metadata/<id>?positionMs=... lookups, or
        #   2) actual segment requests for the new queue, or
        #   3) explicit manual skip/next where publish_current_metadata=True.
        if queue and (publish_current_metadata or channel_id not in self.xtra_metadata):
            self.xtra_metadata[channel_id] = dict(queue[0])
        return rewritten

    def get_channel(self, id):
        # Fetch and rewrite the channel's media playlist. Linear channels keep
        # the original behavior. XTRA channels are returned as a finite stitched
        # VOD playlist containing several prefetched tracks. This keeps players
        # starting at segment 0 while giving them enough queued audio to continue
        # automatically across song boundaries.
        channel_info = self.get_channel_info(id)
        is_xtra = channel_info and channel_info.get("channel_type") == "channel-xtra"

        streaminfo = self.get_tuner(id)
        if not streaminfo:
            self.log("No stream info available for channel {}".format(id))
            return False

        if is_xtra:
            cached = self.xtra_playlists.get(id)
            rebuild_from_next = False

            if cached:
                elapsed = time.time() - cached.get("started", 0)
                total_duration = cached.get("duration", 0) or 0
                segments = cached.get("segments", [])
                served_count = cached.get("served_count", len(cached.get("served", set())))
                consumed_ratio = (served_count / float(len(segments))) if segments else 0.0
                threshold = float(getattr(self, "xtra_extend_threshold", 0.70))
                max_age = int(getattr(self, "xtra_playlist_max_age", 21600))

                if elapsed > max_age or cached.get("served_last", False):
                    self.log("XTRA stitched playlist expired for {}; rebuilding from next track after {:.1f}s".format(id, elapsed))
                    self.xtra_playlists.pop(id, None)
                    rebuild_from_next = True
                elif consumed_ratio >= threshold:
                    # If a client re-requests /listen after consuming most/all of
                    # the stitched queue, do not rebuild from the cached current
                    # stream. Force one peek first so the new queue starts with
                    # the next song instead of repeating the prior queue's final
                    # song.
                    self.log("XTRA queue refresh requested for {} at {:.0%} consumed ({}/{}); rebuilding from next track".format(
                        id, consumed_ratio, served_count, len(segments)
                    ))
                    self.xtra_playlists.pop(id, None)
                    rebuild_from_next = True
                else:
                    return cached["data"]

            if rebuild_from_next:
                next_streaminfo = self.get_tuner(id, force_next=True)
                if next_streaminfo:
                    streaminfo = next_streaminfo
                else:
                    self.log("Unable to advance XTRA queue for {}; rebuilding from current stream".format(id))

            return self._build_and_cache_xtra_queue(id, streaminfo)

        sessionId = streaminfo["sessionId"] if "sessionId" in streaminfo and streaminfo["sessionId"] != None else ''
        aacurl = "{}/{}".format(streaminfo["base_url"], streaminfo["quality"])

        data = self.sfetch(aacurl)
        if not data:
            self.log("Failed to fetch AAC stream list; clearing cached tuner info for {}".format(id))
            self.stream_urls.pop(id, None)
            streaminfo = self.get_tuner(id)
            if not streaminfo:
                return False
            sessionId = streaminfo["sessionId"] if "sessionId" in streaminfo and streaminfo["sessionId"] != None else ''
            aacurl = "{}/{}".format(streaminfo["base_url"], streaminfo["quality"])
            data = self.sfetch(aacurl)
            if not data:
                self.log("Failed to fetch AAC stream list after retune")
                return False

        playlist_text = data.decode("utf-8")
        rewritten = self._rewrite_media_playlist(id, playlist_text, sessionId, is_xtra=False)
        return rewritten

    def get_segment(self, id, seg, sessionId=''):
        try:
            if sessionId != '':
                streaminfo = self.get_tuner_cached(id, sessionId)
            else:
                streaminfo = self.get_tuner(id)

            if not streaminfo:
                return None

            baseurl = streaminfo["base_url"]
            HLStag = streaminfo["HLS"]
            segmenturl = "{}/{}/{}".format(baseurl, HLStag, seg)
            data = self.sfetch(segmenturl)

            channel_info = self.get_channel_info(id)
            is_xtra = channel_info and channel_info.get("channel_type") == "channel-xtra"
            if data and is_xtra:
                cached = self.xtra_playlists.get(id)
                if cached is not None:
                    clean_seg = seg.split('?', 1)[0]
                    served = cached.setdefault("served", set())
                    served.add(clean_seg)
                    cached["served_count"] = len(served)
                    if clean_seg == cached.get("last_segment"):
                        cached["served_last"] = True
                    # Do not publish metadata from segment requests.
                    # HLS clients often prebuffer future XTRA queue segments before
                    # they are actually audible. Publishing metadata here causes
                    # future song metadata to appear early near queue boundaries.
                    #
                    # Metadata should be resolved by M3You using:
                    #   /metadata/<channel_id>?positionMs=<current_player_position_ms>
                    # or explicitly published by manual /xtra/<channel_id>/next.

            if not data:
                if is_xtra:
                    self.log("Segment fetch failed for {}; resetting XTRA session and retuning".format(id))

                    self.xtra_playlists.pop(id, None)
                    self.xtra_state.pop(id, None)
                    self.stream_urls.pop(id, None)

                    fresh_stream = self.get_tuner(id, force_next=False)
                    if not fresh_stream:
                        self.log("Failed to recover XTRA stream after reset for {}".format(id))
                        return None

                    new_playlist_url = "{}/{}".format(fresh_stream["base_url"], fresh_stream["quality"])
                    playlist_data = self.sfetch(new_playlist_url)
                    if not playlist_data:
                        self.log("Failed to fetch playlist after reset for {}".format(id))
                        return None

                    playlist_text = playlist_data.decode("utf-8")

                    for line in playlist_text.splitlines():
                        if line.rstrip().endswith('.aac'):
                            seg_url = "{}/{}/{}".format(fresh_stream["base_url"], fresh_stream["HLS"], line)
                            return self.sfetch(seg_url)

                    return None

                if sessionId == '':
                    # Linear-channel segment URL may have gone stale. Clear cached
                    # tuner data so the next request forces a fresh tune.
                    self.stream_urls.pop(id, None)

            return data
        except KeyError:
            self.log("Missing cached Xtra stream session {}; retuning {}".format(sessionId, id))
            self.stream_urls.pop(id, None)
            return None

    def getAESkey(self,uuid):
        data = self.get("playback/key/v1/{}".format(uuid))
        if not data:
            self.log("AES Key fetch error.")
            return False
        return data["key"]
    

def make_sirius_handler(sxm):
    class SiriusHandler(BaseHTTPRequestHandler):
        def safe_write(self, data):
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                # Media players often open, cancel, and retry requests while
                # probing HLS streams. Do not treat that as a server error.
                pass

        def do_GET(self):
            if self.path.find('.m3u8') > 0:
                data = sxm.get_playlist()
                if data:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/x-mpegURL')
                    self.end_headers()
                    self.safe_write(bytes(data, 'utf-8'))
                    return
                else:
                    self.send_response(500)
                    self.end_headers()
            elif self.path.find('.aac') > 0:
                dirsplit = self.path.split("/")
                id = dirsplit[-2]
                seg = dirsplit[-1]
                data = None
                if self.path.find('?') > 0:
                    contextId = self.path.split("?")[-1]
                    data = sxm.get_segment(id,seg,contextId)
                else:
                    data = sxm.get_segment(id,seg)
                
                if data:
                    self.send_response(200)
                    self.send_header('Content-Type', 'audio/x-aac')
                    self.end_headers()
                    self.safe_write(data)
                else:
                    self.send_response(500)
                    self.end_headers()
            elif self.path.startswith('/xtra/') and (self.path.endswith('/next') or self.path.endswith('/previous') or self.path.endswith('/back')):
                parts = self.path.strip('/').split('/')
                channel_id = parts[1] if len(parts) >= 3 else ''
                action = parts[2] if len(parts) >= 3 else ''
                if action == 'next':
                    meta = sxm.next_xtra_track(channel_id)
                    status = 200 if meta else 404
                else:
                    meta = sxm.previous_xtra_track(channel_id)
                    status = 501 if meta else 404
                if meta:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.safe_write(json.dumps(meta).encode('utf-8'))
                else:
                    self.send_response(status)
                    self.end_headers()
            elif self.path.startswith('/metadata/'):
                parsed = urllib.parse.urlparse(self.path)
                split = parsed.path.split("/")
                channel_id = split[-1]
                params = urllib.parse.parse_qs(parsed.query)
                pos = None
                if "positionMs" in params:
                    try:
                        pos = int(params.get("positionMs", [None])[0])
                    except (TypeError, ValueError):
                        pos = None
                elif "pos" in params:
                    try:
                        pos = int(params.get("pos", [None])[0])
                    except (TypeError, ValueError):
                        pos = None
                include_queue = params.get("queue", ["1"])[0] not in ("0", "false", "False", "no")
                meta = sxm.get_metadata(channel_id, position_ms=pos, include_queue=include_queue)
                if meta:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.safe_write(json.dumps(meta).encode('utf-8'))
                else:
                    self.send_response(404)
                    self.end_headers()
            elif self.path.startswith('/key/'):
                split = self.path.split("/")
                uuid = split[-1]
                key = base64.b64decode(sxm.getAESkey(uuid))
                if not key:
                    self.send_response(500)
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/plain')
                    self.end_headers()
                    self.safe_write(key)
            elif self.path.startswith("/listen/"):
                data = sxm.get_channel(self.path.split('/')[-1])
                if data:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/x-mpegURL')
                    self.end_headers()
                    self.safe_write(data)
                else:
                    self.send_response(500)
                    self.end_headers()
            else:
                self.send_response(500)
                self.end_headers()
    return SiriusHandler



if __name__ == '__main__':
    config_path = os.environ.get('CONFIG_PATH', 'config.ini')
    loaded_configs = config.read(config_path)
    if not loaded_configs:
        print("Error: unable to read config file '{}'.".format(config_path))
        sys.exit(1)

    email = config.get("account", "email", fallback="example@example.com").strip()
    username = config.get("account", "username", fallback="example").strip()
    password = config.get("account", "password").strip()

    ip = config.get("settings", "ip")
    port = int(config.get("settings", "port"))

    # Ignore placeholder/default values
    valid_email = email and email.lower() != "example@example.com"
    valid_username = username and username.lower() != "example"

    if valid_username:
        login_handle = username
    elif valid_email:
        login_handle = email
    else:
        print("Error: please set either a real username or a real email in config.ini")
        sys.exit(1)

    print("Starting server at {}:{}".format(ip, port))

    sxm = SiriusXM(login_handle, password)

    sxm.xtra_stitch_tracks = config.getint(
        "settings",
        "xtra_queue_tracks",
        fallback=int(os.environ.get("XTRA_STITCH_TRACKS", sxm.xtra_stitch_tracks))
    )
    sxm.xtra_extend_threshold = config.getfloat(
        "settings",
        "xtra_extend_threshold",
        fallback=float(os.environ.get("XTRA_EXTEND_THRESHOLD", sxm.xtra_extend_threshold))
    )
    sxm.xtra_playlist_max_age = config.getint(
        "settings",
        "xtra_playlist_max_age",
        fallback=int(os.environ.get("XTRA_PLAYLIST_MAX_AGE", sxm.xtra_playlist_max_age))
    )
    print("XTRA queue settings: tracks={}, extend_threshold={}, max_age={}s".format(
        sxm.xtra_stitch_tracks,
        sxm.xtra_extend_threshold,
        sxm.xtra_playlist_max_age
    ))

    playlist_host = config.get("settings", "playlist_host", fallback=ip).strip()
    if playlist_host == "0.0.0.0":
        playlist_host = "127.0.0.1"

    playlist_scheme = config.get("settings", "playlist_scheme", fallback="http").strip() or "http"
    playlist_port = config.get("settings", "playlist_port", fallback=str(port)).strip()
    if playlist_port:
        playlist_base_url = f"{playlist_scheme}://{playlist_host}:{playlist_port}"
    else:
        playlist_base_url = f"{playlist_scheme}://{playlist_host}"

    playlist = sxm.get_playlist()
    playlist = playlist.replace('/listen/', f'{playlist_base_url}/listen/')

    playlist_output = config.get(
        "settings",
        "playlist_output",
        fallback=os.environ.get("PLAYLIST_OUTPUT", "siriusxm.m3u")
    ).strip()

    playlist_dir = os.path.dirname(playlist_output)
    if playlist_dir:
        os.makedirs(playlist_dir, exist_ok=True)

    with open(playlist_output, "w", encoding="utf-8") as f:
        f.write(playlist)

    print("Saved playlist to {}".format(playlist_output))

    httpd = HTTPServer((ip, port), make_sirius_handler(sxm))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()
