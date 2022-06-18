#!/usr/bin/env python
#
# Figure out which submission most needs to be pushed into a new branch for
# building and testing.  Goals:
#
# 1.  Don't do anything if we're still waiting for build results from too
#     many branches from any given provider.  This limits our resource
#     consumption.
# 2.  The top priority is noticing newly posted patches.  So find the least
#     recent submission whose last message ID has changed since our last
#     branch.
# 3.  If we can't find any of those, then just rebuild every patch at a rate
#     that will get though them all every 48 hours, to check for bitrot.

import cfbot_commitfest_rpc
import cfbot_config
import cfbot_util
import datetime as dt
import glob
import logging
import os
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlparse

def need_to_limit_rate(conn):
  """Have we pushed too many branches recently?"""
  # Don't let any provider finish up with more than the configured maximum
  # number of builds still running.
  cursor = conn.cursor()
  cursor.execute("""SELECT COUNT(*)
                      FROM branch
                     WHERE status = 'testing'""")
  row = cursor.fetchone()
  return row and row[0] >= cfbot_config.CONCURRENT_BUILDS

def choose_submission_with_new_patch(conn):
  """Return the ID pair for the submission most deserving, because it has been
     waiting the longest amongst submissions that have a new patch
     available."""
  # we'll use the last email time as an approximation of the time the patch
  # was sent, because it was most likely that message and it seems like a
  # waste of time to use a more accurate time for the message with the
  # attachment
  # -- wait a couple of minutes before probing because the archives are slow!
  cursor = conn.cursor()
  cursor.execute("""SELECT commitfest_id, submission_id
                      FROM submission
                     WHERE last_message_id IS NOT NULL
                       AND last_message_id IS DISTINCT FROM last_branch_message_id
                       AND status IN ('Ready for Committer', 'Needs review', 'Waiting on Author')
                  ORDER BY last_email_time
                     LIMIT 1""")
  row = cursor.fetchone()
  if row:
    return row
  else:
    return None, None

def choose_submission_without_new_patch(conn):
  """Return the ID pair for the submission that has been waiting longest for
     a periodic bitrot check, but only if we're under the configured rate per
     hour (which is expressed as the cycle time to get through all
     submissions)."""
  # how many submissions are there?
  cursor = conn.cursor()
  cursor.execute("""SELECT COUNT(*)
                      FROM submission
                     WHERE last_message_id IS NOT NULL
                       AND status IN ('Ready for Committer', 'Needs review', 'Waiting on Author')""")
  number, = cursor.fetchone()
  # how many will we need to do per hour to approximate our target rate?
  target_per_hour = number / cfbot_config.CYCLE_TIME
  # are we currently above or below our target rate?
  cursor.execute("""SELECT COUNT(*)
                      FROM submission
                     WHERE last_message_id IS NOT NULL
                       AND status IN ('Ready for Committer', 'Needs review', 'Waiting on Author')
                       AND last_branch_time > now() - INTERVAL '1 hour'""")
  current_rate_per_hour, = cursor.fetchone()
  # is it time yet?
  if current_rate_per_hour < target_per_hour:
    cursor.execute("""SELECT commitfest_id, submission_id
                        FROM submission
                       WHERE last_message_id IS NOT NULL
                         AND status IN ('Ready for Committer', 'Needs review', 'Waiting on Author')
                    ORDER BY last_branch_time NULLS FIRST
                       LIMIT 1""")
    row = cursor.fetchone()
    if row:
      return row
    else:
      return None, None
  else:
    return None, None

def choose_submission(conn):
  """Choose the best submission to process, giving preference to new
     patches."""
  commitfest_id, submission_id = choose_submission_with_new_patch(conn)
  if submission_id:
    return commitfest_id, submission_id
  commitfest_id, submission_id = choose_submission_without_new_patch(conn)
  return commitfest_id, submission_id

def update_patchbase_tree(repo_dir):
  """Pull changes from PostgreSQL master. """
  subprocess.check_call("git am --abort -q 2>/dev/null; git checkout . -q > /dev/null && git clean -fd > /dev/null && git checkout -q master && git pull -q", cwd=repo_dir, shell=True)

def get_commit_id(repo_dir):
  """ return the HEAD commit ID """
  return subprocess.check_output("git rev-list HEAD -1".split(), cwd=repo_dir).decode('utf-8').strip()

def make_branch(conn, patch_dir, **kwargs):
  # compose the commit message
  commit_message = """[CF {commitfest_id}/{submission_id}] {name}

This branch was automatically generated by a robot at cfbot.cputube.org.
It is based on patches submitted to the PostgreSQL mailing lists and
registered in the PostgreSQL Commitfest application.

This branch will be overwritten each time a new patch version is posted to
the email thread, and also periodically to check for bitrot caused by changes
on the master branch.

ci-os-only: html-docs

Commitfest entry: https://commitfest.postgresql.org/{commitfest_id}/{submission_id}
Patch(es): https://www.postgresql.org/message-id/{message_id}
Author(s): {authors}
Base-Branch: {base_branch}
""".format(**kwargs)
  with tempfile.NamedTemporaryFile() as tmp:
    tmp.write(commit_message.encode('utf-8'))
    tmp.flush()
    subprocess.check_call("git commit --allow-empty -q -F".split() + [tmp.name], cwd=patch_dir)

def patchburner_ctl(command, want_rcode=False):
  """Invoke the patchburner control script."""
  if want_rcode:
    p = subprocess.Popen("""%s %s""" % (cfbot_config.PATCHBURNER_CTL, command), shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    stdout, stderr = p.communicate()
    return stdout.decode('utf-8'), p.returncode
  else:
    return subprocess.check_output("%s %s" % (cfbot_config.PATCHBURNER_CTL, command), shell=True).decode('utf-8')

def update_submission(conn, message_id, commit_id, commitfest_id, submission_id):
  # Unfortunately we also have to clobber last_message_id to avoid getting
  # stuck in a loop, because sometimes the commitfest app reports a change
  # in last email date before the new email is visible in the flat thread (!),
  # which means that we can miss a new patch.  Doh.  Need something better
  # here (don't really want to go back to polling threads aggressively...)
  cursor = conn.cursor()
  cursor.execute("""UPDATE submission
                       SET last_message_id = %s,
                           last_branch_message_id = %s,
                           last_branch_commit_id = %s,
                           last_branch_time = now()
                     WHERE commitfest_id = %s AND submission_id = %s""",
                 (message_id, message_id, commit_id, commitfest_id, submission_id))
  

def process_submission(conn, **kwargs):
  commitfest_id = kwargs['commitfest_id']
  submission_id = kwargs['submission_id']
  cursor = conn.cursor()
  thread_url = cfbot_commitfest_rpc.get_thread_url_for_submission(commitfest_id, submission_id)
  if not thread_url:
    # CF entry with no thread attached?
    logging.info("skipping submission %s with no thread" % submission_id)
    return

  logging.info("processing submission %d, %d" % (commitfest_id, submission_id))

  template_repo_path = 'postgresql.cfbot'
  update_patchbase_tree(template_repo_path)
  if os.path.exists(template_repo_path):
    pass # shutil.rmtree(template_repo_path)
  #cmd = 'git clone postgresql.orig'.split() + [template_repo_path]
  #subprocess.check_call(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

  commit_id = get_commit_id(template_repo_path)
  patch_dir = os.path.join(template_repo_path, str(commitfest_id), str(submission_id))
  print('dir', patch_dir)
  os.makedirs(patch_dir, exist_ok=True)
# XXX tmpfile

  # Retrieve and apply all the attachments
  rcode = 0

  message_id, patch_urls = cfbot_commitfest_rpc.get_latest_patches_from_thread_url(thread_url)
  # write the patch output XXX to a public log file
  #log_file = "patch_%d_%d.log" % (commitfest_id, submission_id)
  log_file = os.path.join(patch_dir, 'patch.out')

  patches = []
  for patch_url in patch_urls:
    parsed = urlparse(patch_url)
    filename = os.path.basename(parsed.path)
    patches += [filename]
    dest = os.path.join(patch_dir, filename)
    with open(dest, "wb+") as f:
      f.write(cfbot_util.slow_fetch_binary(patch_url))

  # decompress them XXX is it safe to run these on untrusted input ?
  for patch in patches:
    if patch.endswith('.tgz') or patch.endswith('.tar.gz') or patch.endswith('.tar.bz2'):
      subprocess.check_call(['tar', 'xzf', patch])
    elif patch.endswith('.zip'):
      subprocess.check_call(['unzip', patch])
    elif patch.endswith('.gz'):
      subprocess.check_call(['gunzip', patch])

  patches = glob.glob(os.path.join(patch_dir, '*.diff'))
  patches += glob.glob(os.path.join(patch_dir, '*.patch'))
  patches.sort()

  # make a branch to apply the commits to
  branch = "commitfest/%s/%s" % (commitfest_id, submission_id)
  logging.info("creating branch %s" % branch)
  subprocess.check_call('git checkout -q -f -B'.split() + [branch], cwd=patch_dir)

  with open(log_file, "a+") as log:
    log.write("=== Applying patches on top of PostgreSQL commit ID %s at %s ===\n" %
        (commit_id, dt.datetime.now(dt.timezone.utc).isoformat()))

    for filename in patches:
      cmd = 'git mailinfo ./msg ./patch'.split()
      p = subprocess.Popen(cmd, cwd=patch_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
      stdout , stderr = p.communicate(open(filename, 'rb').read())
      msgsize = os.path.getsize(os.path.join(patch_dir, './msg'))

      if not stdout.strip() and msgsize == 0:
        # The commit message is empty, indicating a raw diff (not git-format-patch)
        cmd = 'git apply --index'.split()
        p = subprocess.Popen(cmd + [os.path.join(str(commitfest_id), str(submission_id), './patch')], cwd=template_repo_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        stdout , stderr = p.communicate()
        log.write(stdout.decode())
        print(stdout.decode())
        rcode = p.returncode

        # Do not commit: the cfbot banner message will be committed later.
        # This gives essentially the same historic behavior for raw patches.
        # It would be confusing to have a single commit with no message and
        # then another commit with a longer message with no patch.
        # (Arguably, it's also confusing if the commit with the cfbot banner
        # is sometimes empty and sometimes not).

        #cmd = ['git', 'commit', '--author=Unset <nobody@cfbot>', '-am', 'No commit message was specified']
        #p = subprocess.Popen(cmd, cwd=template_repo_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        #stdout , stderr = p.communicate()
        #log.write(stdout.decode())
        #rcode = rcode or p.returncode
      else:
        cmd = 'git am --patch-format=mbox'.split()
        p = subprocess.Popen(cmd + [os.path.basename(filename)], cwd=patch_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        stdout , stderr = p.communicate()
        log.write(stdout.decode())
        print(stdout.decode())
        rcode = p.returncode

      if rcode != 0:
        break

  # did "patch" actually succeed?
  if rcode != 0:
    print('we failed to apply the patches: ...', commitfest_id, submission_id)
  else:
    # we applied (and maybe committed) the patches; now put an informational commit on top
    make_branch(conn, patch_dir, **kwargs, message_id=message_id, base_branch=commit_id)

    # push it to the remote monitored repo, if configured
    if cfbot_config.GIT_REMOTE_NAME:
      logging.info("pushing branch %s" % branch)
      my_env = os.environ.copy()
      my_env["GIT_SSH_COMMAND"] = cfbot_config.GIT_SSH_COMMAND
      subprocess.check_call('git push -q -f'.split() + [cfbot_config.GIT_REMOTE_NAME, branch], env=my_env, cwd=patch_dir)
    return True

def maybe_process_one(conn):
  if not need_to_limit_rate(conn):
    commitfest_id, submission_id = choose_submission(conn)
    if submission_id:
      process_submission(conn, commitfest_id=commitfest_id, submission_id=submission_id)
 

if __name__ == "__main__":
  #with cfbot_util.db() as conn:
  #for i in range(3254,4000):
    #maybe_process_one(conn)
    import cfbot_commitfest_rpc
    fooddb = cfbot_commitfest_rpc.foodb()
    process_submission(None, commitfest_id=19, submission_id=1769)
    #process_submission(foodb, 19, 1769) # does not apply
    #process_submission(foodb, 38, 3256) # git format with multiple patches
    process_submission(None, commitfest_id=38, submission_id=3633) # raw patch
    #ret = process_submission(None, 38, i)
    #if ret: break
    #cfbot_commitfest_rpc.get_latest_patches_from_thread_url(get_thread_url_for_submission(37, 2901)
