"""
Tests for VectrixDB API Server and Dashboard.
"""

import pytest


class TestAPIImports:
    """Test API module imports."""

    def test_server_module_exists(self):
        """Test that server module can be imported."""
        try:
            from vectrixdb.api import server
            assert server is not None
        except ImportError as e:
            pytest.skip(f"API module not available: {e}")

    def test_fastapi_app_exists(self):
        """Test that FastAPI app can be imported."""
        try:
            from vectrixdb.api.server import app
            assert app is not None
        except ImportError as e:
            pytest.skip(f"FastAPI not installed: {e}")


class TestDashboardFile:
    """Test dashboard file exists."""

    def test_dashboard_html_exists(self):
        """Test dashboard HTML file exists."""
        from pathlib import Path
        import vectrixdb

        pkg_dir = Path(vectrixdb.__file__).parent
        dashboard_path = pkg_dir / "dashboard" / "index.html"

        assert dashboard_path.exists(), f"Dashboard not found at {dashboard_path}"

    def test_dashboard_contains_vectrixdb(self):
        """Test dashboard HTML contains VectrixDB branding."""
        from pathlib import Path
        import vectrixdb

        pkg_dir = Path(vectrixdb.__file__).parent
        dashboard_path = pkg_dir / "dashboard" / "index.html"

        content = dashboard_path.read_text(encoding='utf-8')
        assert "VectrixDB" in content

    def test_dashboard_has_storage_badge(self):
        """Test dashboard has storage badge element."""
        from pathlib import Path
        import vectrixdb

        pkg_dir = Path(vectrixdb.__file__).parent
        dashboard_path = pkg_dir / "dashboard" / "index.html"

        content = dashboard_path.read_text(encoding='utf-8')
        assert "storage-badge" in content

    def test_dashboard_has_delta_lake_style(self):
        """Test dashboard has Delta Lake CSS style."""
        from pathlib import Path
        import vectrixdb

        pkg_dir = Path(vectrixdb.__file__).parent
        dashboard_path = pkg_dir / "dashboard" / "index.html"

        content = dashboard_path.read_text(encoding='utf-8')
        assert "delta_lake" in content

    def test_dashboard_has_code_tips(self):
        """Test dashboard has Code Tips section."""
        from pathlib import Path
        import vectrixdb

        pkg_dir = Path(vectrixdb.__file__).parent
        dashboard_path = pkg_dir / "dashboard" / "index.html"

        content = dashboard_path.read_text(encoding='utf-8')
        assert "Code Tips" in content or "code-tips" in content

    def test_dashboard_has_sync_section(self):
        """Test dashboard has Sync section in Code Tips."""
        from pathlib import Path
        import vectrixdb

        pkg_dir = Path(vectrixdb.__file__).parent
        dashboard_path = pkg_dir / "dashboard" / "index.html"

        content = dashboard_path.read_text(encoding='utf-8')
        assert "Sync" in content or "sync" in content


class TestAPIEndpointsWithClient:
    """Test API endpoints (requires FastAPI TestClient)."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        try:
            from fastapi.testclient import TestClient
            from vectrixdb.api.server import app
            return TestClient(app)
        except ImportError:
            pytest.skip("FastAPI TestClient not available")

    def test_health_endpoint(self, client):
        """Test health check endpoint."""
        response = client.get("/health")
        assert response.status_code == 200

    def test_api_root(self, client):
        """Test API root endpoint."""
        response = client.get("/api/v1")
        assert response.status_code == 200

    def test_openapi_docs(self, client):
        """Test OpenAPI documentation endpoint."""
        response = client.get("/openapi.json")
        assert response.status_code == 200
        data = response.json()
        assert "openapi" in data
