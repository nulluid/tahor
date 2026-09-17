import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('system_services', ROOT/'scripts/system_services.py')
services = importlib.util.module_from_spec(spec)
spec.loader.exec_module(services)


class SystemServiceTests(unittest.TestCase):
    def test_services_share_restricted_identity_and_writable_state(self):
        units = services.render('tahor',Path('/etc/tahor/config.env'),Path('/var/lib/tahor'),Path('/opt/tahor/venv/bin/python'),Path('/opt/tahor'))
        self.assertEqual(len(units),15)
        for name,text in units.items():
            if name.endswith('.service'):
                for setting in ('User=tahor\n','NoNewPrivileges=true','CapabilityBoundingSet=\n','ProtectSystem=strict','ProtectHome=true','ReadWritePaths="/var/lib/tahor"','LimitCORE=0'):
                    self.assertIn(setting,text)
                self.assertNotIn('/home/',text)
        self.assertIn('09:00:00 UTC',units['tahor-filing.timer'])
        self.assertIn('OnCalendar=*:0/5', units['tahor-decisions.timer'])
        self.assertIn(' decisions ', units['tahor-decisions.service'])
        self.assertIn('Type=oneshot', units['tahor-decisions.service'])

    def test_invalid_identity_and_path_injection_rejected(self):
        for user in ('root','tahor\nUser=root','bad user'):
            with self.assertRaises(ValueError):
                services.render(user,Path('/etc/config'),Path('/var/lib/tahor'),Path('/usr/bin/python3'))
        with self.assertRaises(ValueError):
            services.render('tahor',Path('/etc/config\nExecStart=bad'),Path('/var/lib/tahor'),Path('/usr/bin/python3'))

    @unittest.skipUnless(shutil.which('systemd-analyze'),'Linux systemd parser required')
    def test_real_systemd_parser_accepts_hardening_and_timers(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)
            for name,text in services.render('tahor',Path('/etc/tahor/config.env'),Path('/var/lib/tahor'),Path('/usr/bin/python3'),ROOT).items():
                (output/name).write_text(text)
            result=subprocess.run(['systemd-analyze','verify',*[str(p) for p in output.iterdir()]],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
