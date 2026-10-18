"""READ ELEMENT STATUS, parsed.

Every reply below was captured from a real library on this host rather than
written by hand, because the offsets inside a descriptor move with the flags
requested and the identifier area is a per-personality width. Three
personalities are covered, and they disagree about all three things that
matter: where the drives are, how wide the identifier is, and what is in it.
"""
from unittest import mock

from django.test import TestCase

from apps.libraries.services.operations import elements


def reply(hex_text: str) -> bytes:
    return bytes.fromhex(''.join(hex_text.split()))


#: STK L80, library 30, four drives from element 500, one loaded with
#: G03001TA. dvcid_len 32, serial only, so the identifier IS the serial.
STK = reply("""
01 f4 00 02 00 00 00 b8  04 80 00 58 00 00 00 b0
01 f4 01 00 00 00 20 09  00 81 03 e8 47 30 33 30
30 31 54 41 20 20 20 20  20 20 20 20 20 20 20 20
20 20 20 20 20 20 20 20  20 20 20 20 00 00 00 00
02 00 00 08 58 59 5a 5a  59 5f 42 31 20 20 20 20
20 20 20 20 20 20 20 20  20 20 20 20 20 20 20 20
20 20 20 20 ff ff ff ff  01 f5 08 00 00 00 20 0a
00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00  02 00 00 08 58 59 5a 5a
59 5f 42 32 20 20 20 20  20 20 20 20 20 20 20 20
20 20 20 20 20 20 20 20  20 20 20 20 ff ff ff ff
""")


class ParseTests(TestCase):
    def test_it_finds_every_drive_element(self):
        found = elements.parse(STK)
        self.assertEqual([e.address for e in found], [500, 501])

    def test_it_says_which_drive_holds_a_tape(self):
        loaded, empty = elements.parse(STK)
        self.assertTrue(loaded.full)
        self.assertEqual(loaded.barcode, 'G03001TA')
        self.assertFalse(empty.full)
        self.assertIsNone(empty.barcode)

    def test_it_reads_the_device_identifier(self):
        loaded, empty = elements.parse(STK)
        self.assertEqual(loaded.identifier, 'XYZZY_B1')
        self.assertEqual(empty.identifier, 'XYZZY_B2')

    def test_a_reply_with_no_elements_is_not_an_error(self):
        """What a library answers when asked about a type it has none of."""
        self.assertEqual(elements.parse(reply(
            'ff ff 00 00 00 00 00 08  04 00 00 32 00 00 00 00')), [])

    def test_a_truncated_reply_is_not_an_error(self):
        self.assertEqual(elements.parse(b''), [])
        self.assertEqual(elements.parse(reply('01 f4 00 02')), [])


class IdentifierMatchingTests(TestCase):
    """The identifier is not the serial, and what it holds depends on the
    personality and on the drive. All three forms were measured here."""

    def _element(self, identifier):
        return elements.DriveElement(address=500, full=True,
                                     identifier=identifier)

    def test_a_serial_only_identifier_is_the_serial(self):
        """STK, dvcid_len 32, serial only."""
        self.assertTrue(self._element('XYZZY_B1').is_drive('XYZZY_B1'))

    def test_a_vendor_product_serial_identifier_ends_with_it(self):
        """The default personality's 34 bytes: 8 vendor, 16 product, 10
        serial, which is what a SONY LIB-302 reports here."""
        element = self._element('SONY    SDX-900V        XYZZY_21')
        self.assertTrue(element.is_drive('XYZZY_21'))
        self.assertFalse(element.is_drive('XYZZY_22'))

    def test_a_zero_padded_ibm_serial_still_matches(self):
        """Upstream e43e2e3 zero-pads short IBM LTO serials as the drives do,
        so device.conf says XYZZY_A1 and the library reports 00XYZZY_A1."""
        self.assertTrue(self._element('00XYZZY_A1').is_drive('XYZZY_A1'))

    def test_an_empty_identifier_matches_nothing(self):
        self.assertFalse(self._element(None).is_drive('XYZZY_B1'))
        self.assertFalse(self._element('XYZZY_B1').is_drive(''))


class LoadedByDriveTests(TestCase):
    """What the panel asks for: drive id -> the barcode in it."""

    def _answer(self, serials, data=STK):
        with mock.patch.object(elements.mapping, 'device_for_library',
                               return_value='/dev/sg4'), \
                mock.patch.object(elements.shell, 'sudo_bytes',
                                  return_value=(True, data, '')):
            return elements.loaded_by_drive(30, serials)

    def test_it_keys_the_answer_by_device_conf_id(self):
        self.assertEqual(self._answer({31: 'XYZZY_B1', 32: 'XYZZY_B2'}),
                         {31: 'G03001TA', 32: None})

    def test_a_drive_the_library_did_not_describe_is_left_out(self):
        """Left out rather than reported empty: the caller then knows it does
        not know, instead of being told the drive holds nothing."""
        self.assertNotIn(33, self._answer({33: 'NOT_THERE'}))

    def test_an_ambiguous_serial_is_refused_rather_than_guessed(self):
        """Two elements ending with the same serial decide nothing.

        Position would decide it, and position is what reaches the wrong drive
        when a daemon holds stale state. Two drives should never share a
        serial - device_serial() makes them unique by construction - but
        device.conf can be edited by hand, and a wrong drive is worse than no
        answer.
        """
        both = [elements.DriveElement(500, True, 'AAA001L8', 'SHARED'),
                elements.DriveElement(501, True, 'BBB002L8', 'ALSO_SHARED')]
        with mock.patch.object(elements, 'drives', return_value=both):
            self.assertEqual(elements.loaded_by_drive(30, {31: 'SHARED'}), {})

    def test_a_library_that_cannot_be_asked_answers_nothing(self):
        with mock.patch.object(elements.mapping, 'device_for_library',
                               return_value=None):
            self.assertEqual(elements.loaded_by_drive(30, {31: 'XYZZY_B1'}), {})

    def test_a_failed_command_answers_nothing(self):
        with mock.patch.object(elements.mapping, 'device_for_library',
                               return_value='/dev/sg4'), \
                mock.patch.object(elements.shell, 'sudo_bytes',
                                  return_value=(False, b'', 'no such device')):
            self.assertEqual(elements.loaded_by_drive(30, {31: 'XYZZY_B1'}), {})

    def test_a_unit_attention_is_retried(self):
        """A library has one pending after a daemon restart, and it clears by
        being reported. Without the retry the first poll after every restart
        would say the whole library is empty."""
        answers = [(False, b'', 'sg_raw: Unit attention'), (True, STK, '')]
        with mock.patch.object(elements.mapping, 'device_for_library',
                               return_value='/dev/sg4'), \
                mock.patch.object(elements.shell, 'sudo_bytes',
                                  side_effect=answers) as asked:
            found = elements.loaded_by_drive(30, {31: 'XYZZY_B1'})
        self.assertEqual(asked.call_count, 2)
        self.assertEqual(found, {31: 'G03001TA'})
