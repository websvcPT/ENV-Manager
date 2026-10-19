"""Reading failures: the output of real git and ssh becomes one of the fixed problems.

The samples are what git 2.47 and OpenSSH 10 printed when run against a throwaway server for
each case (a wrong key, a missing repository, a closed port, an unknown or changed host key, a
push behind the remote) and what git prints for https failures. A problem carries a fixed
sentence and never any text that was printed.
"""

import pytest

from app.services.git_errors import PROBLEMS, GitProblem, GitSyncError, classify

CHANGED_KEY = """@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!
Someone could be eavesdropping on you right now (man-in-the-middle attack)!
It is also possible that a host key has just been changed.
The fingerprint for the ED25519 key sent by the remote host is
SHA256:YyUyeDkB19msjKv5TZMBppjQNq0NrXm4+8c0lXF/Vlc.
Please contact your system administrator.
Add correct host key in /tmp/kh3 to get rid of this message.
Offending ECDSA key in /tmp/kh3:1
  remove with:
  ssh-keygen -f '/tmp/kh3' -R '[gitcheck-ssh]:2222'
Host key for [gitcheck-ssh]:2222 has changed and you have requested strict checking.
Host key verification failed.
fatal: Could not read from remote repository.

Please make sure you have the correct access rights
and the repository exists.
"""
UNKNOWN_KEY = (
    "No ED25519 host key is known for [gitcheck-ssh]:2222 and you have requested strict checking.\n"
    "Host key verification failed.\n"
    "fatal: Could not read from remote repository.\n"
)
WRONG_KEY = """git@gitcheck-ssh: Permission denied (publickey,password,keyboard-interactive).
fatal: Could not read from remote repository.

Please make sure you have the correct access rights
and the repository exists.
"""
MISSING = """fatal: '/srv/git/missing.git' does not appear to be a git repository
fatal: Could not read from remote repository.
"""
CLOSED_PORT = """ssh: connect to host gitcheck-ssh port 2299: Connection refused
fatal: Could not read from remote repository.
"""
UNRESOLVED = """ssh: Could not resolve hostname nohost.invalid: Name or service not known
fatal: Could not read from remote repository.
"""
BEHIND = """To ssh://gitcheck-ssh:2222/srv/git/env.git
 ! [rejected]        HEAD -> main (fetch first)
error: failed to push some refs to 'ssh://gitcheck-ssh:2222/srv/git/env.git'
hint: Updates were rejected because the remote contains work that you do not
hint: have locally.
"""
PROTECTED = """remote: error: GH006: Protected branch update failed for refs/heads/main.
 ! [remote rejected] HEAD -> main (protected branch hook declined)
error: failed to push some refs to 'https://git.example.com/ops/env.git'
"""
HTTPS_UNRESOLVED = (
    "fatal: unable to access 'https://nohost.invalid/x/y.git/': "
    "Could not resolve host: nohost.invalid"
)
HTTPS_REFUSED = (
    "fatal: unable to access 'https://gitcheck-ssh:2299/x/y.git/': Failed to connect to "
    "gitcheck-ssh port 2299 after 1 ms: Could not connect to server"
)
HTTPS_TLS = (
    "fatal: unable to access 'https://gitcheck-ssh:2222/x/y.git/': GnuTLS, handshake failed: "
    "An unexpected TLS packet was received."
)
HTTPS_BAD_CREDENTIALS = (
    "remote: Invalid username or password.\n"
    "fatal: Authentication failed for 'https://git.example.com/ops/env.git/'"
)
HTTPS_NO_PROMPT = (
    "fatal: could not read Username for 'https://git.example.com': terminal prompts disabled"
)
HTTPS_FORBIDDEN = (
    "fatal: unable to access 'https://git.example.com/ops/env.git/': The requested URL "
    "returned error: 403"
)
HTTPS_NOT_FOUND = (
    "remote: Repository not found.\nfatal: repository 'https://git.example.com/ops/env.git/' "
    "not found"
)


@pytest.mark.parametrize(
    ("output", "problem"),
    [
        (CHANGED_KEY, GitProblem.HOST_KEY_CHANGED),
        (UNKNOWN_KEY, GitProblem.HOST_KEY_CHANGED),
        (WRONG_KEY, GitProblem.AUTH),
        (MISSING, GitProblem.NOT_FOUND),
        (CLOSED_PORT, GitProblem.NETWORK),
        (UNRESOLVED, GitProblem.NETWORK),
        (BEHIND, GitProblem.STALE),
        (PROTECTED, GitProblem.REJECTED),
        (HTTPS_UNRESOLVED, GitProblem.NETWORK),
        (HTTPS_REFUSED, GitProblem.NETWORK),
        (HTTPS_TLS, GitProblem.NETWORK),
        (HTTPS_BAD_CREDENTIALS, GitProblem.AUTH),
        (HTTPS_NO_PROMPT, GitProblem.AUTH),
        (HTTPS_FORBIDDEN, GitProblem.AUTH),
        (HTTPS_NOT_FOUND, GitProblem.NOT_FOUND),
        ("fatal: something nobody has seen before", GitProblem.FAILED),
        ("", GitProblem.FAILED),
    ],
)
def test_real_output_is_read_as_the_problem_it_describes(output: str, problem: GitProblem) -> None:
    """Each real message maps to its problem, the first matching the order of severity."""
    assert classify(output) is problem


def test_a_command_that_ran_out_of_time_is_a_timeout_whatever_it_printed() -> None:
    """A deadline wins over any text the command printed before it was killed."""
    assert classify(WRONG_KEY, timed_out=True) is GitProblem.TIMEOUT


def test_an_error_carries_the_fixed_sentence_and_whether_waiting_can_help() -> None:
    """The message is the problem's own sentence; only faults that can pass are transient."""
    error = GitSyncError(GitProblem.NETWORK)
    assert str(error) == error.message == PROBLEMS[GitProblem.NETWORK].message
    assert error.transient
    transient = {problem for problem, info in PROBLEMS.items() if info.transient}
    assert transient == {
        GitProblem.NETWORK,
        GitProblem.TIMEOUT,
        GitProblem.STALE,
        GitProblem.FAILED,
    }


def test_no_sentence_names_a_path_a_host_or_a_credential() -> None:
    """The sentences are generic: nothing a person typed or a server printed can be in them."""
    for info in PROBLEMS.values():
        assert len(info.message) <= 200
        assert "/" not in info.message and "@" not in info.message
