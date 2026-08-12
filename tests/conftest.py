"""Test-only bootstrap: meeting_bot reads required env vars and instantiates a
Slack App (which calls auth.test over the network) at import time. Set dummy
env and disable Slack token verification so the module imports offline.
Empty ANTHROPIC_API_KEY -> meeting_bot.client is None (pure-function tests)."""
import os

os.environ.setdefault('SLACK_BOT_TOKEN', 'xoxb-test')
os.environ.setdefault('SLACK_APP_TOKEN', 'xapp-test')
os.environ.setdefault('ANTHROPIC_API_KEY', '')
os.environ.setdefault('HS_API_KEY', 'test-hs-key')

import slack_bolt

_RealApp = slack_bolt.App


class _TestApp(_RealApp):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('token_verification_enabled', False)
        kwargs.setdefault('request_verification_enabled', False)
        super().__init__(*args, **kwargs)


slack_bolt.App = _TestApp
