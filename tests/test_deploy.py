# Copyright (C) 2026 Andreas Schwenk <schwenk@member.fsf.org>
# Created in part with AI help (Claude, Anthropic).
# SPDX-License-Identifier: GPL-3.0-or-later

import copy
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from emptydiff.core import config, deploy

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def prod_cfg(**over):
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg["base_url"] = "https://emptydiff.school.invalid"
    cfg["auth"].update(
        allowed_domains=["students.school.invalid"],
        admins=["Lehrende@school.invalid"],
    )
    cfg["mail"].update(
        host="smtp.school.invalid", sender="emptydiff@school.invalid"
    )
    cfg["privacy"]["log_days"] = 7
    for (
        key,
        value,
    ) in (
        over.items()
    ):  # "limits__max_upload_bytes" sets cfg["limits"]["max_upload_bytes"]
        section, _, name = key.partition("__")
        if name:
            cfg[section][name] = value
        else:
            cfg[section] = value
    return cfg


class DeployTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"EMPTYDIFF_DEV": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = tmp.name

    def render(self, cfg=None, **kw):
        kw.setdefault("accept_acme_terms", True)
        return deploy.render(
            cfg or prod_cfg(), "/opt/emptydiff", self.out, **kw
        )

    def read(self, name):
        with open(os.path.join(self.out, name), encoding="utf-8") as f:
            return f.read()


class RenderTest(DeployTestCase):
    def test_all_files_are_written_without_leftover_placeholders(self):
        written = self.render()
        self.assertEqual(
            sorted(written),
            sorted(
                [
                    *deploy.OUTPUTS.values(),
                    "systemd/var-lib-emptydiff-run.mount",
                ]
            ),
        )
        for name in written:
            self.assertIsNone(re.search(r"@[A-Z_]+@", self.read(name)), name)

    def test_apache_values_come_from_the_configuration(self):
        self.render(prod_cfg(limits__max_upload_bytes=3_000_000))
        conf = self.read("apache/emptydiff.conf")
        self.assertIn("MDomain emptydiff.school.invalid", conf)
        self.assertIn("ServerName emptydiff.school.invalid", conf)
        self.assertIn("MDContactEmail lehrende@school.invalid", conf)
        self.assertIn("MDCertificateAgreement accepted", conf)
        self.assertIn("LimitRequestBody 4000000", conf)
        self.assertRegex(
            conf,
            r'<Location "/admin/backup/upload">\s+'
            r"LimitRequestBody 502000000\s+</Location>",
        )
        self.assertIn("python-path=/opt/emptydiff ", conf)
        self.assertIn(
            "WSGIScriptAlias / /opt/emptydiff/emptydiff/wsgi.py", conf
        )
        self.assertIn("Strict-Transport-Security", conf)
        self.assertIn('X-Robots-Tag "noindex, nofollow"', conf)
        self.assertIn("/var/log/apache2/emptydiff/access.log", conf)

    def test_units_run_as_the_service_user_from_the_install_dir(self):
        self.render()
        for unit in (
            "systemd/emptydiff-worker.service",
            "systemd/emptydiff-purge.service",
        ):
            text = self.read(unit)
            self.assertIn("User=emptydiff", text)
            self.assertIn("WorkingDirectory=/opt/emptydiff", text)
            self.assertIn("PYTHONPATH=/opt/emptydiff", text)
        self.assertIn(
            "ExecStart=/usr/bin/python3 -m emptydiff worker",
            self.read("systemd/emptydiff-worker.service"),
        )
        self.assertIn(
            "Restart=on-failure", self.read("systemd/emptydiff-worker.service")
        )
        self.assertIn(
            "-m emptydiff purge", self.read("systemd/emptydiff-purge.service")
        )
        timer = self.read("systemd/emptydiff-purge.timer")
        self.assertIn("Persistent=true", timer)
        self.assertIn("OnCalendar=", timer)

    def test_run_folders_live_on_a_ram_disk_of_fixed_size(self):
        self.render()
        mount = self.read("systemd/var-lib-emptydiff-run.mount")
        self.assertIn("Where=/var/lib/emptydiff/run", mount)
        self.assertIn("Type=tmpfs", mount)
        for option in ("size=256M", "mode=1777", "nosuid", "nodev", "noexec"):
            self.assertIn(option, mount)
        self.assertIn("WantedBy=local-fs.target", mount)

    def test_the_mount_unit_is_named_after_the_data_directory(self):
        self.render(prod_cfg(data_dir="/srv/emptydiff-data"))
        self.assertIn(
            "Where=/srv/emptydiff-data/run",
            self.read("systemd/srv-emptydiff\\x2ddata-run.mount"),
        )
        self.assertIn(
            "RequiresMountsFor=/srv/emptydiff-data/run",
            self.read("systemd/emptydiff-worker.service"),
        )

    @unittest.skipUnless(
        shutil.which("systemd-escape"), "systemd-escape is not available"
    )
    def test_mount_unit_names_agree_with_systemd(self):
        for path in (
            "/var/lib/emptydiff/run",
            "/srv/emptydiff-data/run",
            "/srv/my_data.dir/run",
            "/srv/.hid/run",
            "/a/b-c/d_e/run",
        ):
            with self.subTest(path=path):
                expected = subprocess.run(
                    ["systemd-escape", "-p", "--suffix=mount", path],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                self.assertEqual(deploy.mount_unit_name(path), expected)

    def test_invalid_data_directories_are_rejected(self):
        for bad in (
            "relative/path",
            "/var/lib/emptydiff space",
            "/var/lib/m;rm",
            "",
        ):
            with self.subTest(bad=bad), self.assertRaises(deploy.DeployError):
                self.render(prod_cfg(data_dir=bad))

    def test_the_worker_is_limited_as_a_whole_and_has_no_network(self):
        self.render()
        text = self.read("systemd/emptydiff-worker.service")
        for line in (
            "MemoryMax=2G",
            "MemorySwapMax=0",
            "TasksMax=200",
            "PrivateNetwork=yes",
            "NoNewPrivileges=yes",
            "RequiresMountsFor=/var/lib/emptydiff/run",
        ):
            self.assertIn(line, text)

    def test_log_retention_follows_privacy_log_days(self):
        cfg = prod_cfg()
        cfg["privacy"]["log_days"] = 3
        self.render(cfg)
        text = self.read("logrotate/emptydiff")
        self.assertIn("rotate 3", text)
        self.assertIn("maxage 3", text)
        self.assertIn("/var/log/apache2/emptydiff/*.log", text)

    def test_acme_terms_must_be_accepted_explicitly(self):
        with self.assertRaises(deploy.DeployError) as ctx:
            self.render(accept_acme_terms=False)
        self.assertIn("--accept-acme-terms", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(self.out, "apache")))

    def test_invalid_base_urls_are_rejected(self):
        for bad in (
            "ftp://emptydiff.school.invalid",
            "emptydiff.school.invalid",
            "https://emptydiff.school.invalid/App",
            "https://emptydiff.school.invalid/a b",
            "https://emptydiff.school.invalid/a//b",
            "https://emptydiff.school.invalid/..",
            "https://emptydiff.school.invalid/app?x=1",
            "https://localhost",
            "https://10.0.0.5",
            "https://emptydiff.school.invalid:8443",
            "https://bad host.invalid",
            "https://UNDER_SCORE.school.invalid",
            "",
        ):
            with self.subTest(bad=bad), self.assertRaises(deploy.DeployError):
                self.render(prod_cfg(base_url=bad))

    def test_plain_http_is_allowed_for_names_and_ipv4_addresses(self):
        for good in (
            "http://emptydiff.school.invalid",
            "http://labserver",
            "http://labserver.local",
            "http://192.0.2.10",
        ):
            with self.subTest(good=good):
                self.render(prod_cfg(base_url=good), accept_acme_terms=False)

    def test_invalid_plain_http_base_urls_are_rejected(self):
        for bad in (
            "http://emptydiff.school.invalid/App",
            "http://emptydiff.school.invalid/a//b",
            "http://emptydiff.school.invalid:8080",
            "http://192.0.2.10:8080",
            "http://999.1.1.1",
            "http://10.0",
            "http://bad host.invalid",
            "http://UNDER_SCORE.school.invalid",
            "http://",
        ):
            with self.subTest(bad=bad), self.assertRaises(deploy.DeployError):
                self.render(prod_cfg(base_url=bad), accept_acme_terms=False)

    def test_plain_http_apache_has_no_tls_and_no_certificate(self):
        self.render(
            prod_cfg(base_url="http://192.0.2.10"),
            accept_acme_terms=False,
        )
        conf = self.read("apache/emptydiff.conf")
        self.assertIn("<VirtualHost *:80>", conf)
        self.assertIn("ServerName 192.0.2.10", conf)
        for tls in (
            ":443",
            "SSLEngine",
            "MDomain",
            "MDCertificateAgreement",
            "Strict-Transport-Security",
        ):
            self.assertNotIn(tls, conf)

    def test_both_apache_variants_share_the_application_directives(self):
        for url, acme in (
            ("https://emptydiff.school.invalid", True),
            ("http://emptydiff.school.invalid", False),
        ):
            with self.subTest(url=url):
                self.render(prod_cfg(base_url=url), accept_acme_terms=acme)
                conf = self.read("apache/emptydiff.conf")
                for line in (
                    "LimitRequestBody 6000000",
                    "WSGIScriptAlias / /opt/emptydiff/emptydiff/wsgi.py",
                    "Alias /static/ /opt/emptydiff/emptydiff/static/",
                    "CustomLog /var/log/apache2/emptydiff/access.log",
                    'X-Robots-Tag "noindex, nofollow"',
                ):
                    self.assertIn(line, conf)
                self.assertEqual(conf.count("WSGIScriptAlias"), 1)
                self.assertNotIn("shared by both", conf)

    def test_base_url_path_is_the_mount_point_of_the_application(self):
        for url in (
            "https://emptydiff.school.invalid/lab",
            "https://emptydiff.school.invalid/lab/",
        ):
            with self.subTest(url=url):
                self.render(prod_cfg(base_url=url))
                conf = self.read("apache/emptydiff.conf")
                self.assertIn(
                    "WSGIScriptAlias /lab /opt/emptydiff/emptydiff/wsgi.py",
                    conf,
                )
                self.assertIn(
                    "Alias /lab/static/ /opt/emptydiff/emptydiff/static/",
                    conf,
                )
                self.assertIn('<Location "/lab/admin/backup/upload">', conf)
        self.render()
        conf = self.read("apache/emptydiff.conf")
        self.assertIn("WSGIScriptAlias / /opt/emptydiff", conf)
        self.assertIn("Alias /static/ /opt/emptydiff", conf)
        self.assertIn('<Location "/admin/backup/upload">', conf)

    def proxy_cfg(self, **over):
        cfg = prod_cfg(base_url="https://pool.school.invalid/lab", **over)
        cfg["proxy"]["addresses"] = ["192.0.2.20", "198.51.100.0/24"]
        return cfg

    def test_behind_a_proxy_only_the_proxy_is_answered_over_plain_http(self):
        self.render(self.proxy_cfg(), accept_acme_terms=False)
        conf = self.read("apache/emptydiff.conf")
        self.assertIn("<VirtualHost *:80>", conf)
        self.assertIn("ServerName pool.school.invalid", conf)
        self.assertRegex(
            conf,
            r'<Location "/">\s+Require ip 192\.0\.2\.20 198\.51\.100\.0/24'
            r"\s+</Location>",
        )
        self.assertIn("WSGIScriptAlias /lab ", conf)
        self.assertIn("Alias /lab/static/ ", conf)
        for tls in (
            ":443",
            "SSLEngine",
            "MDomain",
            "MDCertificateAgreement",
            "Strict-Transport-Security",
        ):
            self.assertNotIn(tls, conf)

    def test_the_apache_site_names_its_mode_for_install_sh(self):
        for cfg, acme, mode in (
            (prod_cfg(), True, "https"),
            (prod_cfg(base_url="http://192.0.2.10"), False, "http"),
            (self.proxy_cfg(), False, "proxy"),
        ):
            with self.subTest(mode=mode):
                self.render(cfg, accept_acme_terms=acme)
                self.assertRegex(
                    self.read("apache/emptydiff.conf"),
                    rf"(?m)^# mode: {mode}$",
                )

    def test_behind_a_proxy_the_public_address_is_https(self):
        cfg = self.proxy_cfg()
        cfg["base_url"] = "http://pool.school.invalid/lab"
        with self.assertRaises(deploy.DeployError):
            self.render(cfg, accept_acme_terms=False)

    def test_unedited_template_values_are_rejected(self):
        for key, value in (("base_url", "https://emptydiff.example.org"),):
            with self.assertRaises(deploy.DeployError) as ctx:
                self.render(prod_cfg(**{key: value}))
            self.assertIn("placeholders", str(ctx.exception))
        cfg = prod_cfg()
        cfg["mail"]["host"] = "smtp.example.org"
        with self.assertRaises(deploy.DeployError):
            self.render(cfg)
        cfg = prod_cfg()
        cfg["auth"]["admins"] = ["dozent@example.edu"]
        with self.assertRaises(deploy.DeployError):
            self.render(cfg)

    def test_mandatory_settings_are_required(self):
        cfg = prod_cfg()
        cfg["auth"]["admins"] = []
        with self.assertRaises(deploy.DeployError) as ctx:
            self.render(cfg)
        self.assertIn("auth.admins", str(ctx.exception))

    def test_unsafe_install_dirs_are_rejected(self):
        for bad in (
            "relative/dir",
            "/opt/ma thlab",
            "/opt/emptydiff;rm",
            "/opt/$HOME",
            "",
        ):
            with self.subTest(bad=bad), self.assertRaises(deploy.DeployError):
                deploy.render(
                    prod_cfg(), bad, self.out, accept_acme_terms=True
                )

    def test_shipped_production_template_parses_and_is_flagged_as_unedited(
        self,
    ):
        with open(
            os.path.join(ROOT, "deploy", "config.production.toml"), "rb"
        ) as f:
            import tomllib

            data = tomllib.load(f)
        cfg = config._merge(copy.deepcopy(config.DEFAULTS), data)
        config._validate(cfg)  # the template is a valid configuration
        with self.assertRaises(deploy.DeployError) as ctx:
            deploy.render(
                cfg, "/opt/emptydiff", self.out, accept_acme_terms=True
            )
        self.assertIn("placeholders", str(ctx.exception))


class WsgiEntryTest(unittest.TestCase):
    def test_entry_uses_an_absolute_import(self):
        """mod_wsgi loads wsgi.py without package context; a relative
        import led to HTTP 500 in the Apache test."""
        with open(
            os.path.join(ROOT, "emptydiff", "wsgi.py"), encoding="utf-8"
        ) as f:
            text = f.read()
        self.assertIn("from emptydiff.web import create_app", text)
        self.assertNotRegex(text, r"(?m)^from \.")
        self.assertIn("application = create_app()", text)


class InstallScriptTest(unittest.TestCase):
    def test_script_has_valid_syntax_and_is_executable(self):
        path = os.path.join(ROOT, "install.sh")
        self.assertTrue(
            os.access(path, os.X_OK), "install.sh is not executable"
        )
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash not available")
        subprocess.run([bash, "-n", path], check=True)

    def test_script_never_touches_user_data_or_config(self):
        with open(os.path.join(ROOT, "install.sh"), encoding="utf-8") as f:
            text = f.read()
        for dangerous in (
            "rm -rf /var/lib",
            "rm -r /var/lib",
            'rm -rf "$DATA_DIR',
            "> /etc/emptydiff/config.toml",
        ):
            self.assertNotIn(dangerous, text)


if __name__ == "__main__":
    unittest.main()
