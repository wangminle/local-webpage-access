"""Node 安全基线门禁：验证旧补丁、最低修复版本与异常输出。"""

import subprocess

import pytest

from local_webpage_access.doctor import STATUS_OK, STATUS_WARN
from local_webpage_access.setup import _check_node


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("v24.22.1", STATUS_OK),
        ("v24.22.0", STATUS_WARN),
        ("v24.21.9", STATUS_WARN),
        ("v22.23.4", STATUS_WARN),
        ("v24.22.1-rc.1", STATUS_WARN),
        ("invalid", STATUS_WARN),
    ],
)
def test_node_security_patch_gate(monkeypatch, version, expected):
    """实际 setup 门禁必须拒绝低于修复基线及无法识别的版本。"""
    monkeypatch.setattr("local_webpage_access.setup.shutil.which", lambda _: "/test/node")
    result = _check_node(
        lambda args: subprocess.CompletedProcess(args, 0, stdout=version, stderr=""),
        "linux",
    )
    assert result.status == expected
    assert "24.22.1" in result.required

