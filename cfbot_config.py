import logging
import sys

# which CI providers are enabled
CI_MODULES = ("appveyor", "cirrus",)
CI_PROVIDERS = ("appveyor", "cirrus/windows", "cirrus/freebsd", "cirrus/linux", "cirrus/macos")

# http settings (be polite by identifying ourselves and limited rate)
SLOW_FETCH_SLEEP = 0 #1.0
USER_AGENT = "PostgreSQL patch tester at http://cfbot.cputube.org"
TIMEOUT = 10

LOCK_FILE="/tmp/cfbot-lock"

# database settings
DSN="dbname=cfbot host=/tmp"

# patch settings
PATCHBURNER_CTL="sudo ./cfbot_patchburner_chroot_ctl.sh"
CYCLE_TIME = 48.0
CONCURRENT_BUILDS = 1

# travis settings
TRAVIS_USER="macdice"
TRAVIS_REPO="postgres"
TRAVIS_API_BUILDS="https://api.travis-ci.org/repos/" + TRAVIS_USER + "/" + TRAVIS_REPO + "/builds"
TRAVIS_BUILD_URL="https://travis-ci.org/" + TRAVIS_USER + "/" + TRAVIS_REPO + "/builds/%s"

# appveyor settings
APPVEYOR_USER="macdice"
APPVEYOR_REPO="postgres"
APPVEYOR_API_BUILDS="https://ci.appveyor.com/api/projects/"+ APPVEYOR_USER + "/" + APPVEYOR_REPO + "/history?recordsNumber=100"
APPVEYOR_BUILD_URL="https://ci.appveyor.com/project/"+ APPVEYOR_USER + "/" + APPVEYOR_REPO + "/build/%s"

# cirus settings
CIRRUS_USER="macdice"
CIRRUS_REPO="postgres"

# git settings
#GIT_SSH_COMMAND="ssh -i ~/.ssh/cfbot_github_rsa"
GIT_SSH_COMMAND="ssh"
#GIT_REMOTE_NAME="cfbot-repo"
#GIT_REMOTE_NAME="justinpryzby/postgres"
GIT_REMOTE_NAME="github"

# http output
WEB_ROOT="/home/pryzbyj/public_html/cfbot"
CFBOT_APPLY_URL="https://cfbot.cputube.org/log/%s"

# log settings
logging.basicConfig(format='%(asctime)s %(message)s', filename="cfbot.log", level=logging.INFO)

