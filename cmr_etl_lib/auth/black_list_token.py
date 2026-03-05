import datetime

from ..document import Document


class BlacklistToken(Document):
    """
    Token Model for storing JWT tokens
    """
    __TABLE__ = "blacklist"

    token = None
    blacklisted_on = None

    def __init__(self, token):
        self.token = token
        self.blacklisted_on = datetime.datetime.now()

    def __repr__(self):
        return '<id: token: {}'.format(self.token)

    @staticmethod
    def check_blacklist(auth_token):
        # check whether auth token has been blacklisted
        res = BlacklistToken(None).load({'token': str(auth_token)})
        if res.id:
            return True
        else:
            return False
