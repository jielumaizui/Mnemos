"""Dependency installation helpers for setup."""

from __future__ import annotations

import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


@dataclass(frozen=True)
class DependencyInstallOutcome:
    ok: bool
    python_exe: str
    reexec_script: Path | None = None
    reexec_argv: tuple[str, ...] = ()

    @property
    def should_reexec(self) -> bool:
        return self.reexec_script is not None


def ensure_venv(
    *,
    project_root: Path,
    python_executable: str,
    print_err: Callable[[str], None],
    print_warn: Callable[[str], None],
) -> Optional[Path]:
    """Create or reuse repo .venv and return its Python executable."""
    venv_dir = project_root / ".venv"
    if not venv_dir.exists():
        print("  创建虚拟环境 .venv ...")
        result = subprocess.run(
            [python_executable, "-m", "venv", str(venv_dir)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            print_err(f"创建虚拟环境失败: {result.stderr[:200]}")
            return None
    if platform.system() == "Windows":
        venv_python = venv_dir / "Scripts" / "python.exe"
    else:
        venv_python = venv_dir / "bin" / "python"
    if not venv_python.exists():
        print_err(f"虚拟环境 Python 未找到: {venv_python}")
        return None

    print("  升级虚拟环境 pip/setuptools/wheel ...")
    try:
        result = subprocess.run(
            [str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            print_warn(f"pip/setuptools/wheel 升级失败，继续使用现有虚拟环境: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        print_warn("pip/setuptools/wheel 升级超时，继续使用现有虚拟环境")
    return venv_python


def _try_install(
    python: str,
    *,
    project_root: Path,
    timeout: int,
    silent: bool = False,
    no_build_isolation: bool = False,
) -> tuple[bool, str]:
    command = [python, "-m", "pip", "install", "-e", str(project_root)]
    if no_build_isolation:
        command.insert(4, "--no-build-isolation")
    if silent:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            return False, f"dependency install timed out after {exc.timeout} seconds"
        return result.returncode == 0, result.stderr

    print("  开始安装依赖，预计需要 1-3 分钟（取决于网络速度）...")
    print("  默认安装仅包含运行依赖；向量索引加速依赖可稍后用 pip install -e '.[ml]' 安装。")
    try:
        result = subprocess.run(command, timeout=timeout, text=True)
    except subprocess.TimeoutExpired as exc:
        return False, f"dependency install timed out after {exc.timeout} seconds"
    if result.returncode == 0:
        return True, ""
    try:
        retry = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return False, f"dependency install timed out after {exc.timeout} seconds"
    return False, retry.stderr


def _looks_like_build_dependency_failure(stderr: str) -> bool:
    text = stderr.lower()
    return (
        "installing build dependencies did not run successfully" in text
        or "failed to build" in text
        or "no matching distribution found for wheel" in text
    )


def install_dependencies(
    *,
    project_root: Path,
    python_exe: str,
    reexec_args: list[str] | None,
    reexec_entrypoint: str | None,
    ensure_venv_func: Callable[[], Optional[Path]],
    print_ok: Callable[[str], None],
    print_warn: Callable[[str], None],
    print_err: Callable[[str], None],
) -> DependencyInstallOutcome:
    print("  安装项目依赖...")
    pyproject = project_root / "pyproject.toml"
    if not pyproject.exists():
        print_err("未找到 pyproject.toml，无法安装依赖")
        return DependencyInstallOutcome(False, python_exe)

    timeout = 600
    ok, err = _try_install(python_exe, project_root=project_root, timeout=timeout)
    if ok:
        print_ok("依赖安装完成")
        return DependencyInstallOutcome(True, python_exe)

    if "externally-managed-environment" in err or "externally managed" in err:
        print_warn("检测到系统 Python 外部管理限制，尝试创建虚拟环境...")
        venv_python = ensure_venv_func()
        if venv_python:
            venv_python_str = str(venv_python)
            ok, err2 = _try_install(venv_python_str, project_root=project_root, timeout=timeout)
            if not ok and _looks_like_build_dependency_failure(err2):
                print_warn("构建依赖隔离安装失败，使用现有虚拟环境重试 --no-build-isolation")
                ok, err2 = _try_install(
                    venv_python_str,
                    project_root=project_root,
                    timeout=timeout,
                    no_build_isolation=True,
                )
            if ok:
                print_ok(f"依赖已在虚拟环境安装: {venv_python}")
                print("  使用虚拟环境 Python 重新执行脚本...")
                argv = list(reexec_args if reexec_args is not None else sys.argv[1:])
                if "--venv-reexec" not in argv:
                    argv.append("--venv-reexec")
                script = Path(reexec_entrypoint or Path(__file__).resolve())
                return DependencyInstallOutcome(
                    True,
                    venv_python_str,
                    reexec_script=script,
                    reexec_argv=tuple(argv),
                )
            print_err(f"虚拟环境安装失败: {err2[:200]}")
            return DependencyInstallOutcome(False, venv_python_str)
        print_err("无法创建虚拟环境")
        return DependencyInstallOutcome(False, python_exe)

    print_warn(f"安装失败，5秒后重试: {err[:200]}")
    import time

    time.sleep(5)
    ok2, err2 = _try_install(python_exe, project_root=project_root, timeout=timeout)
    if ok2:
        print_ok("依赖安装完成")
        return DependencyInstallOutcome(True, python_exe)
    print_err(f"安装失败: {err2[:200]}")
    return DependencyInstallOutcome(False, python_exe)
