from pathlib import Path
from textwrap import dedent

import pytest


def test_model_call_ledger_audit_rejects_unreserved_direct_provider_call(tmp_path: Path):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    source = tmp_path / "unsafe_provider.py"
    source.write_text(
        """
import requests

def unsafe_call():
    return requests.post('https://provider.example/v1/rerank')
""",
        encoding="utf-8",
    )

    report = audit_model_call_ledger([source])

    assert report["ok"] is False
    assert report["billable_calls_without_ledger"] == 1
    assert "pre_dispatch_reservation" in report["violations"][0]["missing"]


def test_model_call_ledger_audit_accepts_complete_boundary_contract(tmp_path: Path):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    source = tmp_path / "safe_provider.py"
    source.write_text(
        """
import requests

def safe_call(ledger):
    reservation = ledger.reserve()
    try:
        reservation.mark_dispatched()
        response = requests.post('https://provider.example/v1/rerank')
        reservation.settle()
        return response
    except Exception:
        if reservation.dispatched:
            reservation.preserve_incurred()
        else:
            reservation.release()
        raise
""",
        encoding="utf-8",
    )

    report = audit_model_call_ledger([source])

    assert report["ok"] is True
    assert report["billable_calls_without_ledger"] == 0


def test_model_call_ledger_audit_rejects_unrelated_reservation_lifecycle(tmp_path: Path):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    source = tmp_path / "split_reservation.py"
    source.write_text(
        """
import requests

def unsafe_call(ledger):
    dispatched = ledger.reserve()
    settled = ledger.reserve()
    dispatched.mark_dispatched()
    response = requests.post('https://provider.example/v1/rerank')
    settled.settle()
    settled.release()
    settled.preserve_incurred()
    return response
""",
        encoding="utf-8",
    )

    report = audit_model_call_ledger([source])

    assert report["ok"] is False
    assert "settlement_or_handoff" in report["violations"][0]["missing"]


@pytest.mark.parametrize(
    ("name", "source", "expected_missing"),
    [
        (
            "constant_false",
            """
            import requests
            def unsafe_call(ledger):
                reservation = ledger.reserve()
                reservation.mark_dispatched()
                requests.post('https://provider.example/v1/rerank')
                if False:
                    reservation.settle()
                    reservation.release()
                    reservation.preserve_incurred()
            """,
            "settlement_or_handoff",
        ),
        (
            "branch_split",
            """
            import requests
            def unsafe_call(ledger, enabled):
                if enabled:
                    reservation = ledger.reserve()
                    reservation.mark_dispatched()
                else:
                    requests.post('https://provider.example/v1/rerank')
                reservation.settle()
                reservation.release()
                reservation.preserve_incurred()
            """,
            "pre_dispatch_reservation",
        ),
        (
            "conditional_reserve",
            """
            import requests
            def unsafe_call(ledger, enabled):
                if enabled:
                    reservation = ledger.reserve()
                    reservation.mark_dispatched()
                requests.post('https://provider.example/v1/rerank')
                reservation.settle()
            """,
            "pre_dispatch_reservation",
        ),
        (
            "early_return",
            """
            import requests
            def unsafe_call(ledger):
                reservation = ledger.reserve()
                reservation.mark_dispatched()
                response = requests.post('https://provider.example/v1/rerank')
                return response
                reservation.settle()
            """,
            "settlement_or_handoff",
        ),
        (
            "terminal_before_sink",
            """
            import requests
            def unsafe_call(ledger):
                reservation = ledger.reserve()
                reservation.mark_dispatched()
                reservation.settle()
                requests.post('https://provider.example/v1/rerank')
            """,
            "dispatch_mark",
        ),
        (
            "except_only",
            """
            import requests
            def unsafe_call(ledger):
                reservation = ledger.reserve()
                try:
                    reservation.mark_dispatched()
                    requests.post('https://provider.example/v1/rerank')
                except Exception:
                    if reservation.dispatched:
                        reservation.preserve_incurred()
                    else:
                        reservation.release()
            """,
            "settlement_or_handoff",
        ),
        (
            "loop_may_not_execute",
            """
            import requests
            def unsafe_call(ledger, values):
                for value in values:
                    reservation = ledger.reserve()
                    reservation.mark_dispatched()
                requests.post('https://provider.example/v1/rerank')
                reservation.settle()
            """,
            "pre_dispatch_reservation",
        ),
        (
            "requests_request",
            """
            import requests
            def unsafe_call():
                return requests.request('POST', 'https://provider.example/v1/rerank')
            """,
            "pre_dispatch_reservation",
        ),
    ],
)
def test_model_call_ledger_audit_rejects_path_only_contracts(
    tmp_path: Path, name: str, source: str, expected_missing: str
):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    path = tmp_path / f"{name}.py"
    path.write_text(dedent(source), encoding="utf-8")

    report = audit_model_call_ledger([path])

    assert report["ok"] is False
    assert any(expected_missing in item["missing"] for item in report["violations"])


def test_model_call_ledger_audit_rejects_reused_reservation_and_dead_handoff(tmp_path: Path):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    source = tmp_path / "reuse_and_dead_handoff.py"
    source.write_text(
        dedent(
            """
            import requests

            def unsafe_reuse(ledger):
                reservation = ledger.reserve()
                try:
                    reservation.mark_dispatched()
                    requests.post('https://provider.example/v1/rerank')
                    requests.post('https://provider.example/v1/rerank')
                    reservation.settle()
                except Exception:
                    if reservation.dispatched:
                        reservation.preserve_incurred()
                    else:
                        reservation.release()

            def unsafe_dead_handoff(ledger):
                reservation = ledger.reserve()
                try:
                    reservation.mark_dispatched()
                    requests.post('https://provider.example/v1/rerank')
                    if False:
                        return {"_ledger_reservation": reservation}
                except Exception:
                    if reservation.dispatched:
                        reservation.preserve_incurred()
                    else:
                        reservation.release()
            """
        ),
        encoding="utf-8",
    )

    report = audit_model_call_ledger([source])

    assert report["ok"] is False
    assert any("reservation_reused" in item["missing"] for item in report["violations"])
    assert any("settlement_or_handoff" in item["missing"] for item in report["violations"])


def test_model_call_ledger_audit_accepts_safe_generic_requests_request_boundary(tmp_path: Path):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    source = tmp_path / "safe_generic_request.py"
    source.write_text(
        dedent(
            """
            import requests

            def safe_call(ledger):
                reservation = ledger.reserve()
                try:
                    reservation.mark_dispatched()
                    response = requests.request('POST', 'https://provider.example/v1/rerank')
                    reservation.settle()
                    return response
                except Exception:
                    if reservation.dispatched:
                        reservation.preserve_incurred()
                    else:
                        reservation.release()
                    raise
            """
        ),
        encoding="utf-8",
    )

    report = audit_model_call_ledger([source])

    assert report["ok"] is True


def test_model_call_ledger_audit_rejects_inner_handler_that_swallows_dispatched_call(
    tmp_path: Path,
):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    source = tmp_path / "inner_handler_swallow.py"
    source.write_text(
        dedent(
            """
            import requests

            def unsafe_call(ledger):
                reservation = ledger.reserve()
                try:
                    try:
                        reservation.mark_dispatched()
                        requests.post('https://provider.example/v1/rerank')
                        reservation.settle()
                    except ValueError:
                        pass
                except Exception:
                    if reservation.dispatched:
                        reservation.preserve_incurred()
                    else:
                        reservation.release()
            """
        ),
        encoding="utf-8",
    )

    report = audit_model_call_ledger([source])

    assert report["ok"] is False
    assert any("exception_terminal_lifecycle" in item["missing"] for item in report["violations"])


def test_model_call_ledger_audit_rejects_outer_swallow_after_narrow_inner_handler(
    tmp_path: Path,
):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    source = tmp_path / "outer_handler_swallow.py"
    source.write_text(
        dedent(
            """
            import requests

            def unsafe_call(ledger):
                reservation = ledger.reserve()
                try:
                    try:
                        reservation.mark_dispatched()
                        requests.post('https://provider.example/v1/rerank')
                        reservation.settle()
                    except ValueError:
                        if reservation.dispatched:
                            reservation.preserve_incurred()
                        else:
                            reservation.release()
                except Exception:
                    pass
            """
        ),
        encoding="utf-8",
    )

    report = audit_model_call_ledger([source])

    assert report["ok"] is False
    assert any("exception_terminal_lifecycle" in item["missing"] for item in report["violations"])


@pytest.mark.parametrize(
    ("name", "source", "kind"),
    [
        (
            "import_alias",
            """
            from requests import post as provider_post
            def unsafe_call():
                provider_post('https://provider.example/v1/rerank')
            """,
            "http_post",
        ),
        (
            "assignment_alias",
            """
            import requests
            def unsafe_call():
                send = requests.post
                send('https://provider.example/v1/rerank')
            """,
            "http_post",
        ),
        (
            "getattr",
            """
            import requests
            def unsafe_call():
                getattr(requests, 'post')('https://provider.example/v1/rerank')
            """,
            "http_post",
        ),
        (
            "session_inline",
            """
            import requests
            def unsafe_call():
                requests.Session().post('https://provider.example/v1/rerank')
            """,
            "http_post",
        ),
        (
            "session_alias",
            """
            import requests
            def unsafe_call():
                session = requests.Session()
                session.post('https://provider.example/v1/rerank')
            """,
            "http_post",
        ),
        (
            "session_getattr",
            """
            import requests
            def unsafe_call():
                session = requests.Session()
                getattr(session, 'post')('https://provider.example/v1/rerank')
            """,
            "http_post",
        ),
        (
            "httpx",
            """
            import httpx
            def unsafe_call():
                httpx.post('https://provider.example/v1/rerank')
            """,
            "http_post",
        ),
        (
            "httpx_client",
            """
            import httpx
            def unsafe_call():
                client = httpx.Client()
                client.post('https://provider.example/v1/rerank')
            """,
            "http_post",
        ),
        (
            "module_alias_request",
            """
            import requests as req
            def unsafe_call():
                req.request('POST', 'https://provider.example/v1/rerank')
            """,
            "http_request",
        ),
        (
            "local_import_alias",
            """
            def unsafe_call():
                import requests as transport
                transport.post('https://provider.example/v1/rerank')
            """,
            "http_post",
        ),
        (
            "local_import_session",
            """
            def unsafe_call():
                from requests import Session as ProviderSession
                session = ProviderSession()
                session.post('https://provider.example/v1/rerank')
            """,
            "http_post",
        ),
        (
            "local_import_dynamic_getattr",
            """
            def unsafe_call():
                import requests as transport
                method = 'post'
                getattr(transport, method)('https://provider.example/v1/rerank')
            """,
            "http_request",
        ),
    ],
)
def test_model_call_ledger_audit_detects_transport_aliases(
    tmp_path: Path, name: str, source: str, kind: str
):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    path = tmp_path / f"{name}.py"
    path.write_text(dedent(source), encoding="utf-8")

    report = audit_model_call_ledger([path])

    assert report["ok"] is False
    assert any(item["kind"] == kind for item in report["violations"])


@pytest.mark.parametrize(
    ("name", "source", "kind"),
    [
        (
            "chat_alias",
            """
            from openai import OpenAI
            def unsafe_call():
                client = OpenAI()
                send = client.chat.completions.create
                send(model='x', messages=[])
            """,
            "chat_completion",
        ),
        (
            "chat_getattr",
            """
            from openai import OpenAI
            def unsafe_call():
                client = OpenAI()
                send = getattr(client.chat.completions, 'create')
                send(model='x', messages=[])
            """,
            "chat_completion",
        ),
        (
            "embedding_alias",
            """
            def unsafe_call(client):
                send = client.embeddings.create
                send(model='x', input=['x'])
            """,
            "embedding",
        ),
        (
            "response_getattr",
            """
            def unsafe_call(client):
                getattr(client.responses, 'create')(model='x', input='x')
            """,
            "response",
        ),
    ],
)
def test_model_call_ledger_audit_detects_openai_callable_aliases(
    tmp_path: Path, name: str, source: str, kind: str
):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    path = tmp_path / f"{name}.py"
    path.write_text(dedent(source), encoding="utf-8")

    report = audit_model_call_ledger([path])

    assert report["ok"] is False
    assert any(item["kind"] == kind for item in report["violations"])


def test_model_call_ledger_audit_rejects_unallowlisted_openai_resource_even_with_ledger(
    tmp_path: Path,
):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    source = tmp_path / "unsupported_openai_image.py"
    source.write_text(
        dedent(
            """
            from openai import OpenAI

            def unsafe_call(ledger):
                client = OpenAI()
                reservation = ledger.reserve()
                try:
                    reservation.mark_dispatched()
                    response = client.images.generate(model='x', prompt='test')
                    reservation.settle()
                    return response
                except Exception:
                    if reservation.dispatched:
                        reservation.preserve_incurred()
                    else:
                        reservation.release()
                    raise
            """
        ),
        encoding="utf-8",
    )

    report = audit_model_call_ledger([source])

    assert report["ok"] is False
    violation = next(
        item
        for item in report["violations"]
        if item["kind"] == "unsupported_openai_resource"
    )
    assert violation["missing"] == ["unsupported_openai_resource"]


def test_model_call_ledger_audit_accepts_allowlisted_openai_resource_with_ledger(
    tmp_path: Path,
):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    source = tmp_path / "allowlisted_openai_chat.py"
    source.write_text(
        dedent(
            """
            from openai import OpenAI

            def safe_call(ledger):
                client = OpenAI()
                reservation = ledger.reserve()
                try:
                    reservation.mark_dispatched()
                    response = client.chat.completions.create(model='x', messages=[])
                    reservation.settle()
                    return response
                except Exception:
                    if reservation.dispatched:
                        reservation.preserve_incurred()
                    else:
                        reservation.release()
                    raise
            """
        ),
        encoding="utf-8",
    )

    report = audit_model_call_ledger([source])

    assert report["ok"] is True


@pytest.mark.parametrize(
    "source",
    [
        """
        def unsafe_call(client):
            return client.images.generate(model='x', prompt='test')
        """,
        """
        def unsafe_call(client):
            images = client.images
            return getattr(images, 'generate')(model='x', prompt='test')
        """,
    ],
)
def test_model_call_ledger_audit_rejects_injected_unallowlisted_openai_resource(
    tmp_path: Path, source: str
):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    path = tmp_path / "injected_unsupported_openai_resource.py"
    path.write_text(dedent(source), encoding="utf-8")

    report = audit_model_call_ledger([path])

    assert report["ok"] is False
    assert any(
        item["kind"] == "unsupported_openai_resource"
        and "unsupported_openai_resource" in item["missing"]
        for item in report["violations"]
    )


@pytest.mark.parametrize(
    ("name", "source", "kind"),
    [
        (
            "dynamic_http_member",
            """
            import requests
            def unsafe_call():
                method = 'post'
                getattr(requests, method)('https://provider.example/v1/rerank')
            """,
            "http_request",
        ),
        (
            "module_assignment_alias",
            """
            import requests
            def unsafe_call():
                transport = requests
                transport.post('https://provider.example/v1/rerank')
            """,
            "http_post",
        ),
        (
            "partial_alias",
            """
            import requests
            from functools import partial
            def unsafe_call():
                send = partial(requests.post, 'https://provider.example/v1/rerank')
                send()
            """,
            "http_post",
        ),
        (
            "dynamic_chat_member",
            """
            def unsafe_call(client):
                method = 'create'
                getattr(client.chat.completions, method)(model='x', messages=[])
            """,
            "chat_completion",
        ),
        (
            "resource_alias",
            """
            def unsafe_call(client):
                endpoint = client.chat.completions
                endpoint.create(model='x', messages=[])
            """,
            "chat_completion",
        ),
        (
            "resource_getattr",
            """
            def unsafe_call(client):
                endpoint = client.chat.completions
                getattr(endpoint, 'create')(model='x', messages=[])
            """,
            "chat_completion",
        ),
    ],
)
def test_model_call_ledger_audit_detects_dynamic_provider_indirection(
    tmp_path: Path, name: str, source: str, kind: str
):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    path = tmp_path / f"{name}.py"
    path.write_text(dedent(source), encoding="utf-8")

    report = audit_model_call_ledger([path])

    assert report["ok"] is False
    assert any(item["kind"] == kind for item in report["violations"])


def test_model_call_ledger_audit_rejects_unknown_exception_factory(tmp_path: Path):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    source = tmp_path / "bogus_exception_factory.py"
    source.write_text(
        dedent(
            """
            import requests

            def bogus_exception_types():
                return (ValueError,)

            def unsafe_call(ledger):
                reservation = ledger.reserve()
                try:
                    reservation.mark_dispatched()
                    requests.post('https://provider.example/v1/rerank')
                    reservation.settle()
                except bogus_exception_types():
                    if reservation.dispatched:
                        reservation.preserve_incurred()
                    else:
                        reservation.release()
            """
        ),
        encoding="utf-8",
    )

    report = audit_model_call_ledger([source])

    assert report["ok"] is False
    assert any("exception_terminal_lifecycle" in item["missing"] for item in report["violations"])


def test_model_call_ledger_audit_rejects_provider_lambda_without_its_own_boundary(
    tmp_path: Path,
):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    source = tmp_path / "provider_lambda.py"
    source.write_text(
        dedent(
            """
            import requests

            def unsafe_call():
                send = lambda: requests.post('https://provider.example/v1/rerank')
                return send()
            """
        ),
        encoding="utf-8",
    )

    report = audit_model_call_ledger([source])

    assert report["ok"] is False
    assert any(item["function"] == "<lambda>" for item in report["violations"])


@pytest.mark.parametrize("exception_name", ["openai_error_type", "RequestException", "OSError"])
def test_model_call_ledger_audit_rejects_shadowed_provider_exception_names(
    tmp_path: Path, exception_name: str
):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    source = tmp_path / f"shadowed_{exception_name}.py"
    source.write_text(
        dedent(
            f"""
            import requests

            def unsafe_call(ledger):
                {exception_name} = ValueError
                reservation = ledger.reserve()
                try:
                    reservation.mark_dispatched()
                    requests.post('https://provider.example/v1/rerank')
                    reservation.settle()
                except {exception_name}:
                    if reservation.dispatched:
                        reservation.preserve_incurred()
                    else:
                        reservation.release()
            """
        ),
        encoding="utf-8",
    )

    report = audit_model_call_ledger([source])

    assert report["ok"] is False
    assert any("exception_terminal_lifecycle" in item["missing"] for item in report["violations"])


def test_model_call_ledger_audit_accepts_proven_openai_error_factory(tmp_path: Path):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    source = tmp_path / "proven_openai_error.py"
    source.write_text(
        dedent(
            """
            import openai
            import requests

            def safe_call(ledger):
                openai_error_type = getattr(openai, 'OpenAIError', RuntimeError)
                reservation = ledger.reserve()
                try:
                    reservation.mark_dispatched()
                    response = requests.post('https://provider.example/v1/rerank')
                    reservation.settle()
                    return response
                except (openai_error_type, OSError):
                    if reservation.dispatched:
                        reservation.preserve_incurred()
                    else:
                        reservation.release()
                    raise
            """
        ),
        encoding="utf-8",
    )

    report = audit_model_call_ledger([source])

    assert report["ok"] is True


def test_model_call_ledger_audit_accepts_any_proven_openai_error_alias(tmp_path: Path):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    source = tmp_path / "proven_openai_error_alias.py"
    source.write_text(
        dedent(
            """
            def safe_call(client, ledger):
                provider_error_type: type[Exception]
                try:
                    from openai import OpenAIError
                    provider_error_type = OpenAIError
                except ImportError:
                    provider_error_type = RuntimeError
                reservation = ledger.reserve()
                try:
                    reservation.mark_dispatched()
                    response = client.embeddings.create(model='m', input=['safe'])
                    reservation.settle()
                    return response
                except (provider_error_type, OSError):
                    if reservation.dispatched:
                        reservation.preserve_incurred()
                    else:
                        reservation.release()
                    raise
            """
        ),
        encoding="utf-8",
    )

    report = audit_model_call_ledger([source])

    assert report["ok"] is True


def test_model_call_ledger_audit_scans_untracked_script_directory(tmp_path: Path):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    source = scripts_dir / "new_provider_path.py"
    source.write_text(
        dedent(
            """
            import requests

            def unsafe_call():
                return requests.post('https://provider.example/v1/rerank')
            """
        ),
        encoding="utf-8",
    )

    report = audit_model_call_ledger([scripts_dir])

    assert report["ok"] is False
    assert report["billable_calls_without_ledger"] == 1


def test_model_call_ledger_audit_rejects_global_and_parameter_exception_shadowing(tmp_path: Path):
    from scripts.audit_model_call_ledger import audit_model_call_ledger

    source = tmp_path / "global_and_parameter_shadow.py"
    source.write_text(
        dedent(
            """
            from openai import OpenAIError
            import requests

            OpenAIError = ValueError

            def unsafe_global(ledger):
                reservation = ledger.reserve()
                try:
                    reservation.mark_dispatched()
                    requests.post('https://provider.example/v1/rerank')
                    reservation.settle()
                except OpenAIError:
                    if reservation.dispatched:
                        reservation.preserve_incurred()
                    else:
                        reservation.release()

            def unsafe_parameter(ledger, OSError):
                reservation = ledger.reserve()
                try:
                    reservation.mark_dispatched()
                    requests.post('https://provider.example/v1/rerank')
                    reservation.settle()
                except OSError:
                    if reservation.dispatched:
                        reservation.preserve_incurred()
                    else:
                        reservation.release()
            """
        ),
        encoding="utf-8",
    )

    report = audit_model_call_ledger([source])

    assert report["ok"] is False
    assert report["billable_calls_without_ledger"] == 2
    assert all("exception_terminal_lifecycle" in item["missing"] for item in report["violations"])
