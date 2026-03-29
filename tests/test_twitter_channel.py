# -*- coding: utf-8 -*-

from unittest.mock import patch, Mock

from agent_reach.channels.twitter import TwitterChannel


def _cp(stdout="", stderr="", returncode=0):
    m = Mock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


def test_check_bird_found_and_auth_ok():
    """bird found + bird check returns 0 → ok."""
    channel = TwitterChannel()
    with patch("shutil.which", side_effect=lambda name: "/usr/local/bin/bird" if name == "bird" else None), patch(
        "subprocess.run",
        return_value=_cp(stdout="Authenticated as @user\n", returncode=0),
    ):
        status, message = channel.check()
    assert status == "ok"
    assert "完整可用" in message


def test_check_bird_found_auth_missing():
    """bird found + bird check returns 1 with 'Missing credentials' → warn about auth."""
    channel = TwitterChannel()
    with patch("shutil.which", side_effect=lambda name: "/usr/local/bin/bird" if name == "bird" else None), patch(
        "subprocess.run",
        return_value=_cp(stderr="Missing credentials: AUTH_TOKEN and CT0 required\n", returncode=1),
    ):
        status, message = channel.check()
    assert status == "warn"
    assert "未配置认证" in message


def test_check_bird_not_found():
    """bird not found → warn with install hint for @steipete/bird."""
    channel = TwitterChannel()
    with patch("shutil.which", return_value=None):
        status, message = channel.check()
    assert status == "warn"
    assert "@steipete/bird" in message


def test_check_birdx_binary_accepted():
    """birdx symlink is accepted as an alternative binary name."""
    channel = TwitterChannel()
    with patch("shutil.which", side_effect=lambda name: "/usr/local/bin/birdx" if name == "birdx" else None), patch(
        "subprocess.run",
        return_value=_cp(stdout="Authenticated as @user\n", returncode=0),
    ):
        status, message = channel.check()
    assert status == "ok"
    assert "完整可用" in message


def test_check_bird_auth_failure_generic():
    """bird check returns 1 without 'Missing credentials' → generic auth failure warn."""
    channel = TwitterChannel()
    with patch("shutil.which", side_effect=lambda name: "/usr/local/bin/bird" if name == "bird" else None), patch(
        "subprocess.run",
        return_value=_cp(stderr="Error: token expired\n", returncode=1),
    ):
        status, message = channel.check()
    assert status == "warn"
    assert "认证检查失败" in message
