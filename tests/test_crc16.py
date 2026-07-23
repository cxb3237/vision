from protocol.crc16 import crc16_ccitt_false
def test_vector():assert crc16_ccitt_false(b"123456789")==0x29B1
def test_empty():assert crc16_ccitt_false(b"")==0xFFFF
def test_consistent():assert crc16_ccitt_false(b"abc"+b"def")==crc16_ccitt_false(b"abcdef")
