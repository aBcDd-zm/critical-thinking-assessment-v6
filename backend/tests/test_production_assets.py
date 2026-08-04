from __future__ import annotations

import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _text(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_production_compose_isolates_v6_and_uses_the_shared_gateway_network() -> None:
    compose = _text("docker-compose.production.yml")
    frontend_service = compose.split("  frontend:\n", 1)[1]

    assert "name: cta-v6" in compose
    assert "cta-v6-sqlite-data" in compose
    assert 'DATABASE_URL: sqlite:////app/data/v6.db' in compose
    assert '"127.0.0.1:18060:8080"' in compose
    assert "external: true" in compose
    assert "name: tencent_default" in compose
    assert "cta-v6-web" in compose
    assert "condition: service_healthy" in compose
    assert "read_only: true" in compose
    assert "healthcheck:" in compose
    assert "DEEPSEEK_API_KEY" not in frontend_service
    assert "ADMIN_TOKEN" not in frontend_service


def test_nginx_keeps_admin_review_private_and_streaming_unbuffered() -> None:
    nginx = _text("frontend/nginx/default.conf.template")
    frontend_dockerfile = _text("frontend/Dockerfile")
    frontend_http = _text("frontend/src/api/http.ts")

    assert "auth_basic" not in nginx
    assert '${ADMIN_TOKEN}' not in nginx
    assert "location ^~ /api/v1/admin" in nginx
    assert "location = /admin" in nginx
    assert "return 404;" in nginx
    assert "turns:stream$" in nginx
    assert "/finalize$" in nginx
    assert "proxy_buffering off" in nginx
    assert "proxy_request_buffering off" in nginx
    assert "limit_req zone=turn_submit" in nginx
    assert "ARG VITE_API_BASE_URL=/api/v1" in frontend_dockerfile
    assert "ADMIN_TOKEN" not in frontend_http
    assert "client_body_temp_path /tmp/nginx/client" in nginx
    assert "10-runtime-dirs.sh" in frontend_dockerfile
    assert "10-admin-basic-auth.sh" not in frontend_dockerfile


def test_tencent_caddy_fragment_is_additive_and_targets_only_v6() -> None:
    caddy = _text("deploy/tencent/Caddyfile.thinkagent.asia")

    assert "thinkagent.asia" in caddy
    assert "sslip.io" not in caddy
    assert "reverse_proxy cta-v6-web:8080" in caddy
    assert "turns:stream$" in caddy
    assert "flush_interval -1" in caddy
    assert "reverse_proxy 127.0.0.1" not in caddy
    assert "Do NOT" in caddy


def test_production_example_keeps_deepseek_out_of_the_frontend_build() -> None:
    example = _text(".env.production.example")
    dockerfile = _text("frontend/Dockerfile")

    assert "DEEPSEEK_API_KEY=REPLACE_" in example
    assert "ADMIN_TOKEN=REPLACE_" in example
    assert "SITE_BASIC_PASSWORD" not in example
    assert "DEEPSEEK_API_KEY" not in dockerfile


def test_deployment_scripts_are_valid_shell_without_triggering_server_actions() -> None:
    for relative_path in (
        "backend/docker-entrypoint.sh",
        "frontend/nginx/10-runtime-dirs.sh",
        "deploy/tencent/deploy.sh",
        "deploy/tencent/backup-sqlite.sh",
    ):
        subprocess.run(
            ["sh", "-n", str(PROJECT_ROOT / relative_path)],
            check=True,
            capture_output=True,
            text=True,
        )

    deploy_script = _text("deploy/tencent/deploy.sh")
    backup_script = _text("deploy/tencent/backup-sqlite.sh")
    assert "reload-caddy" in deploy_script
    assert "caddy validate" in deploy_script
    assert "caddy reload" in deploy_script
    assert "docker cp" in backup_script
    assert "source.backup" in backup_script
