"""Dependency security policy contracts."""

from pathlib import Path
import tomllib

from packaging.requirements import Requirement
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[2]
MIN_SAFE_PYPDF = Version("6.14.2")
MIN_SAFE_CRYPTOGRAPHY = Version("48.0.1")


def _pyproject_dependencies() -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return list(data["project"]["dependencies"])


def _requirements_dependencies() -> list[str]:
    deps: list[str] = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            deps.append(stripped)
    return deps


def _find_requirement(dependencies: list[str], name: str) -> Requirement:
    for dependency in dependencies:
        requirement = Requirement(dependency)
        if requirement.name == name:
            return requirement
    raise AssertionError(f"Missing requirement for {name}")


def _assert_pypdf_security_floor(requirement: Requirement) -> None:
    assert requirement.specifier.contains(MIN_SAFE_PYPDF, prereleases=False)
    assert not requirement.specifier.contains(Version("5.9.0"), prereleases=False)


def _assert_cryptography_security_floor(requirement: Requirement) -> None:
    assert requirement.specifier.contains(MIN_SAFE_CRYPTOGRAPHY, prereleases=False)
    assert not requirement.specifier.contains(Version("43.0.3"), prereleases=False)


def test_pyproject_pypdf_allows_current_safe_release_line():
    requirement = _find_requirement(_pyproject_dependencies(), "pypdf")

    _assert_pypdf_security_floor(requirement)


def test_requirements_pypdf_allows_current_safe_release_line():
    requirement = _find_requirement(_requirements_dependencies(), "pypdf")

    _assert_pypdf_security_floor(requirement)


def test_optional_cryptography_extras_allow_current_safe_release_line():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    optional_dependencies = data["project"]["optional-dependencies"]

    for extra in ("encryption", "dev", "all"):
        requirement = _find_requirement(optional_dependencies[extra], "cryptography")
        _assert_cryptography_security_floor(requirement)
