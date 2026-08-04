from __future__ import annotations

import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _text(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_production_compose_explicitly_refuses_v6_deployment() -> None:
    compose = _text("docker-compose.production.yml")
    assert "cta-v6-local-only" in compose
    assert "no-deployment-authorized" in compose
    assert "services:" not in compose
    assert "image:" not in compose
    assert "DATABASE_URL" not in compose


def test_nginx_keeps_admin_token_server_side_and_streaming_unbuffered() -> None:
    nginx = _text("frontend/nginx/default.conf.template")
    frontend_dockerfile = _text("frontend/Dockerfile")
    frontend_http = _text("frontend/src/api/http.ts")

    assert 'auth_basic "Siheng V6 review"' in nginx
    assert 'proxy_set_header X-Admin-Token "' in nginx
    assert "ADMIN_API_TOKEN" in nginx
    assert "location ^~ /api/v1/admin" in nginx
    assert "turns:stream$" in nginx
    assert "/finalize$" in nginx
    assert "proxy_buffering off" in nginx
    assert "proxy_request_buffering off" in nginx
    assert "limit_req zone=turn_submit" in nginx
    assert "ARG VITE_API_BASE_URL=/api/v1" in frontend_dockerfile
    assert "ADMIN_API_TOKEN" not in frontend_http
    assert "client_body_temp_path /tmp/nginx/client" in nginx


def test_deployment_scripts_are_valid_shell_and_refuse_side_effects() -> None:
    for relative_path in (
        "backend/docker-entrypoint.sh",
        "frontend/nginx/10-admin-basic-auth.sh",
        "deploy/tencent/deploy.sh",
        "deploy/tencent/backup-sqlite.sh",
    ):
        subprocess.run(
            ["sh", "-n", str(PROJECT_ROOT / relative_path)],
            check=True,
            capture_output=True,
            text=True,
        )

    for relative_path in ("deploy/tencent/deploy.sh", "deploy/tencent/backup-sqlite.sh"):
        result = subprocess.run(
            [str(PROJECT_ROOT / relative_path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 64
        assert "Refusing" in result.stderr
