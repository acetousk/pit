import unittest
from unittest.mock import patch

import pi_sidebar as pit


def report(state, extra=b''):
    return b'\x1b]7501;app=pi:state=' + state + extra + b'\x07'


class ProgramStatusTests(unittest.TestCase):
    def track(self, chunks):
        with patch.object(pit.os, 'read', side_effect=[*chunks, b'']), \
                patch.object(pit, 'tmux') as tmux, \
                patch.object(pit, 'completion_sound') as sound:
            pit.program_status('%1', '/tmp/test-tmux')
        return tmux, sound

    def test_completion_rings_once_per_run(self):
        tmux, sound = self.track([b''.join(report(state) for state in (
            b'idle', b'working', b'working', b'done', b'done', b'idle',
            b'working', b'idle'))])
        self.assertEqual(sound.call_count, 2)
        self.assertEqual(tmux.call_count, 8)
        tmux.assert_called_with('set-option', '-p', '-t', '%1', '@pit_status',
                                'idle', check=False, socket='/tmp/test-tmux')

    def test_startup_and_clear_do_not_ring(self):
        _, sound = self.track([b''.join(report(state) for state in (
            b'done', b'idle', b'working', b'clear', b'idle'))])
        sound.assert_not_called()

    def test_blocked_does_not_ring_until_finished(self):
        _, sound = self.track([report(b'working'), report(b'blocked')])
        sound.assert_not_called()
        _, sound = self.track([report(b'working'), report(b'blocked'), report(b'done')])
        sound.assert_called_once_with()

    def test_error_does_not_create_false_completion(self):
        _, sound = self.track([report(b'working'), report(b'error'), report(b'idle')])
        sound.assert_not_called()

    def test_fragmented_reports_and_st_terminator(self):
        payload = report(b'working') + report(b'done').replace(b'\x07', b'\x1b\\')
        tmux, sound = self.track([bytes([byte]) for byte in payload])
        self.assertEqual(tmux.call_count, 2)
        sound.assert_called_once_with()

    def test_other_apps_and_subagents_are_ignored(self):
        tmux, sound = self.track([
            report(b'working', b':id=child'), report(b'done', b':id=child'),
            report(b'working').replace(b'app=pi', b'app=other'), report(b'done')])
        self.assertEqual(tmux.call_count, 1)
        sound.assert_not_called()


class CompletionSoundTests(unittest.TestCase):
    @patch.object(pit.subprocess, 'Popen')
    @patch.object(pit.shutil, 'which', return_value='/usr/bin/canberra-gtk-play')
    def test_theme_sound(self, which, popen):
        pit.completion_sound()
        self.assertEqual(popen.call_args.args[0], ['canberra-gtk-play', '-i', 'complete'])

    @patch.object(pit.subprocess, 'Popen')
    @patch.object(pit.Path, 'is_file', return_value=True)
    @patch.object(pit.shutil, 'which', side_effect=[None, '/usr/bin/pw-play'])
    def test_pipewire_fallback(self, which, is_file, popen):
        pit.completion_sound()
        self.assertEqual(popen.call_args.args[0], [
            '/usr/bin/pw-play', '/usr/share/sounds/freedesktop/stereo/complete.oga'])

    @patch.object(pit.subprocess, 'Popen')
    @patch.object(pit.shutil, 'which', return_value=None)
    def test_missing_players_are_optional(self, which, popen):
        pit.completion_sound()
        popen.assert_not_called()

    @patch.object(pit.subprocess, 'Popen', side_effect=OSError('unavailable'))
    @patch.object(pit.shutil, 'which', return_value='/usr/bin/canberra-gtk-play')
    def test_launch_failure_is_optional(self, which, popen):
        pit.completion_sound()


if __name__ == '__main__':
    unittest.main()
