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
        self.assertEqual(len(units),21)
        for name,text in units.items():
            if name.endswith('.service'):
                for setting in ('User=tahor\n','NoNewPrivileges=true','CapabilityBoundingSet=\n','ProtectSystem=strict','ProtectHome=true','ReadWritePaths="/var/lib/tahor"','LimitCORE=0'):
                    self.assertIn(setting,text)
                self.assertNotIn('/home/',text)
        self.assertIn('OnUnitInactiveSec=5min', units['tahor-filing-maintenance.timer'])
        self.assertIn('TimeoutStartSec=300', units['tahor-filing-maintenance.service'])
        self.assertIn('Type=oneshot', units['tahor-filing-maintenance.service'])
        for name in ('tahor-decision-suggestions', 'tahor-decision-actions'):
            self.assertIn('Type=oneshot', units[name+'.service'])
            self.assertIn('TimeoutStartSec=300', units[name+'.service'])
            self.assertIn('OnUnitInactiveSec=15s', units[name+'.timer'])
            self.assertIn('AccuracySec=1s', units[name+'.timer'])
        self.assertIn('09:00:00 UTC',units['tahor-filing.timer'])
        self.assertIn('OnCalendar=*:0/5', units['tahor-decisions.timer'])
        self.assertIn(' decisions ', units['tahor-decisions.service'])
        self.assertIn('Type=oneshot', units['tahor-decisions.service'])

    def test_user_installer_includes_bounded_five_minute_maintenance(self):
        import setup_tahor
        with tempfile.TemporaryDirectory() as folder:
            target=Path(folder)
            setup_tahor.write_units(target,Path('/config/env'),Path('/usr/bin/python3'))
            self.assertIn('OnUnitInactiveSec=5min',(target/'tahor-filing-maintenance.timer').read_text())
            service=(target/'tahor-filing-maintenance.service').read_text()
            for option in ('Type=oneshot','TimeoutStartSec=300','NoNewPrivileges=true','filing-maintenance'):
                self.assertIn(option,service)

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
