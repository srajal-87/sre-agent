"""Suite-wide safety net for the api tests.

``POST /investigate`` now launches a real investigation unless told otherwise.
A test that posts to it without overriding ``get_investigator`` would make a
live, paid model call - and it very nearly did: before this fixture existed,
the endpoint tests in ``test_investigations.py`` spent three seconds trying to
reach Bedrock.

So auto-investigation is **off for every test by default**, and the tests that
want the run turn it on themselves, alongside the fake investigator that makes
it safe.
"""

import pytest

from app import config


@pytest.fixture(autouse=True)
def no_accidental_investigations():
    previous = config.AGENT_AUTO_INVESTIGATE
    config.AGENT_AUTO_INVESTIGATE = False
    yield
    config.AGENT_AUTO_INVESTIGATE = previous
