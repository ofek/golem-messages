import os
import sys
import struct
import hashlib
import bitcoin
import coincurve
from rlp import utils as rlp_utils
from _pysha3 import sha3_256 as _sha3_256  # pylint: disable=no-name-in-module

from . import exceptions

# https://github.com/ethereum/pydevp2p/blob/
# 8d7c44633ddcc9a00396c9f111f1427f89781b8b/devp2p/crypto.py

CIPHERNAMES = set(('aes-128-ctr',))
if sys.platform not in ('darwin', 'win32'):
    import pyelliptic
elif sys.platform == 'darwin':
    # FIX PATH ON OS X ()
    # https://github.com/yann2192/pyelliptic/issues/11
    _openssl_lib_paths = ['/usr/local/Cellar/openssl/']
    for p in _openssl_lib_paths:
        if os.path.exists(p):
            p = os.path.join(p, os.listdir(p)[-1], 'lib')
            os.environ['DYLD_LIBRARY_PATH'] = p
            import pyelliptic
            if CIPHERNAMES.issubset(set(pyelliptic.Cipher.get_all_cipher())):
                break
elif sys.platform == 'win32':
    has_openssl = True
    try:
        import pyelliptic
        if not CIPHERNAMES.issubset(set(pyelliptic.Cipher.get_all_cipher())):
            raise Exception("Required cyphers not found")
    except Exception as e:  # pylint: disable=broad-except
        has_openssl = False

    if not has_openssl:
        if not getattr(sys, 'frozen', False):
            # Running source
            print('Failed to load openssl, please add it to your PATH.')
            sys.exit(1)
        # USE APP DIR FOR WINDOWS DLL ()
        # https://github.com/golemfactory/golem/issues/1612
        _openssl_lib_paths = [os.path.dirname(sys.executable)]
        for p in _openssl_lib_paths:
            if os.path.exists(p):
                tmp_path = os.environ['PATH'].split(';')
                if p in tmp_path:
                    tmp_path.remove(p)
                tmp_path.insert(0, p)
                os.environ['PATH'] = ';'.join(tmp_path)
                import pyelliptic
                if CIPHERNAMES.issubset(
                        set(pyelliptic.Cipher.get_all_cipher())):
                    break

if 'pyelliptic' not in dir() \
        or not CIPHERNAMES.issubset(set(pyelliptic.Cipher.get_all_cipher())):
    print('required ciphers %r not available in openssl library' % CIPHERNAMES)
    if sys.platform == 'darwin':
        print('use homebrew or macports to install newer openssl')
        print('> brew install openssl / > sudo port install openssl')
    sys.exit(1)


def verify_pubkey(key):
    if len(key) != 64:
        raise exceptions.InvalidKeys('Invalid pubkey length')


def privtopub(raw_privkey):
    raw_pubkey = bitcoin.encode_pubkey(
        bitcoin.privtopub(raw_privkey),
        'bin_electrum'
    )
    verify_pubkey(raw_pubkey)
    return raw_pubkey


def eciesKDF(key_material, key_len):
    """
    interop w/go ecies implementation

    for sha3, blocksize is 136 bytes
    for sha256, blocksize is 64 bytes

    NIST SP 800-56a Concatenation Key Derivation Function (see section 5.8.1).
    """
    s1 = b""
    key = b""
    hash_blocksize = 64
    reps = ((key_len + 7) * 8) / (hash_blocksize * 8)
    counter = 0
    while counter <= reps:
        counter += 1
        ctx = hashlib.sha256()
        ctx.update(struct.pack('>I', counter))
        ctx.update(key_material)
        ctx.update(s1)
        key += ctx.digest()
    return key[:key_len]


def sha3(seed):
    """ Return sha3-256 (NOT keccak) of seed in digest
    :param str seed: data that should be hashed
    :return str: binary hashed data
    """
    if isinstance(seed, str):
        seed = seed.encode()
    return _sha3_256(seed).digest()


def mk_privkey(seed):
    """ Return sha3-256 (keccak) of seed in digest
    TODO: Remove keccak when decoupled from ethereum privatekey
    :param str seed: data that should be hashed
    :return str: binary hashed data
    """
    def sha3_256_kec(x):
        from Crypto.Hash import keccak
        return keccak.new(digest_bits=256, data=rlp_utils.str_to_bytes(x))
    return sha3_256_kec(seed).digest()


def ecdsa_sign(privkey, msghash):
    pk = coincurve.PrivateKey(privkey)
    msghash = sha3(msghash)
    return pk.sign_recoverable(msghash, hasher=None)


def ecdsa_verify(pubkey, signature, message):
    verify_pubkey(pubkey)
    message = sha3(message)
    try:
        pk = coincurve.PublicKey.from_signature_and_message(
            signature, message, hasher=None
        )
    except Exception as e:
        raise exceptions.CoincurveError() from e
    if not pk.format(compressed=False) == b'\04' + pubkey:
        raise exceptions.InvalidSignature()
    return True


class ECCx(pyelliptic.ECC):

    """
    Modified to work with raw_pubkey format used in RLPx
    and binding default curve and cipher
    """
    ecies_ciphername = 'aes-128-ctr'
    curve = 'secp256k1'
    ecies_encrypt_overhead_length = 113

    def __init__(self, raw_privkey):
        if raw_privkey is not None:
            raw_pubkey = privtopub(raw_privkey)
            verify_pubkey(raw_pubkey)
            _, pubkey_x, pubkey_y, _ = self._decode_pubkey(raw_pubkey)
        else:
            raw_pubkey, pubkey_x, pubkey_y = (None,)*3
        while True:
            super().__init__(pubkey_x=pubkey_x, pubkey_y=pubkey_y,
                             raw_privkey=raw_privkey, curve=self.curve)
            # XXX: when raw_privkey is generated by pyelliptic it sometimes
            #      has 31 bytes so we try again!
            if self.raw_privkey and len(self.raw_privkey) != 32:
                continue
            if not self.has_valid_keys():
                raise exceptions.CryptoError('Invalid privkey and/or pubkey')
            break  # init ok

    @property
    def raw_pubkey(self):
        if self.pubkey_x and self.pubkey_y:
            return rlp_utils.str_to_bytes(self.pubkey_x + self.pubkey_y)
        return self.pubkey_x + self.pubkey_y

    @classmethod
    def _decode_pubkey(cls, raw_pubkey):  # pylint: disable=arguments-differ
        verify_pubkey(raw_pubkey)
        pubkey_x = raw_pubkey[:32]
        pubkey_y = raw_pubkey[32:]
        return cls.curve, pubkey_x, pubkey_y, 64

    @property
    def raw_privkey(self):
        if not self.privkey:
            return self.privkey
        return rlp_utils.str_to_bytes(self.privkey)

    def has_valid_keys(self):
        try:
            try:
                # failed for some keys
                bitcoin.get_privkey_format(self.raw_privkey)
            except AssertionError:
                raise exceptions.InvalidKeys('Invalid privkey')
            verify_pubkey(self.raw_pubkey)
            raw_check_result = self.raw_check_key(
                self.raw_privkey,
                *self._decode_pubkey(self.raw_pubkey)[1:3],
            )
            if raw_check_result != 0:
                raise exceptions.InvalidKeys()
        except Exception:  # pylint: disable=broad-except
            return False
        return True

    @classmethod
    def ecies_encrypt(cls, data, raw_pubkey, shared_mac_data=''):
        """
        ECIES Encrypt, where P = recipient public key is:
        1) generate r = random value
        2) generate shared-secret = kdf( ecdhAgree(r, P) )
        3) generate R = rG [same op as generating a public key]
        4) send 0x04 || R || AsymmetricEncrypt(shared-secret, plaintext) || tag


        currently used by go:
        ECIES_AES128_SHA256 = &ECIESParams{
            Hash: sha256.New,
            hashAlgo: crypto.SHA256,
            Cipher: aes.NewCipher,
            BlockSize: aes.BlockSize,
            KeyLen: 16,
            }

        """
        # 1) generate r = random value
        ephem = cls(None)

        # 2) generate shared-secret = kdf( ecdhAgree(r, P) )
        key_material = ephem.raw_get_ecdh_key(
            pubkey_x=raw_pubkey[:32], pubkey_y=raw_pubkey[32:]
        )
        assert len(key_material) == 32
        key = eciesKDF(key_material, 32)
        assert len(key) == 32
        key_enc, key_mac = key[:16], key[16:]

        key_mac = hashlib.sha256(key_mac).digest()  # !!!
        assert len(key_mac) == 32
        # 3) generate R = rG [same op as generating a public key]
        # ephem.raw_pubkey

        # encrypt
        iv = pyelliptic.Cipher.gen_IV(cls.ecies_ciphername)
        assert len(iv) == 16
        ctx = pyelliptic.Cipher(key_enc, iv, 1, cls.ecies_ciphername)
        ciphertext = ctx.ciphering(data)
        assert len(ciphertext) == len(data)

        # 4) send 0x04 || R || AsymmetricEncrypt(shared-secret, plaintext)
        #    || tag
        msg = rlp_utils.ascii_chr(0x04) + ephem.raw_pubkey + iv + ciphertext

        # the MAC of a message (called the tag) as per SEC 1, 3.5.
        tag = pyelliptic.hmac_sha256(
            key_mac, msg[1 + 64:] + rlp_utils.str_to_bytes(shared_mac_data)
        )
        assert len(tag) == 32
        msg += tag

        assert len(msg) == 1 + 64 + 16 + 32 + len(data) == 113 + len(data)
        assert len(msg) - cls.ecies_encrypt_overhead_length == len(data)
        return msg

    def ecies_decrypt(self, data, shared_mac_data=b''):
        """
        Decrypt data with ECIES method using the local private key

        ECIES Decrypt (performed by recipient):
        1) generate shared-secret = kdf( ecdhAgree(myPrivKey, msg[1:65]) )
        2) verify tag
        3) decrypt

        ecdhAgree(r, recipientPublic) == ecdhAgree(recipientPrivate, R)
        [where R = r*G, and recipientPublic = recipientPrivate*G]

        """
        if data[:1] != b'\x04':
            raise exceptions.DecryptionError("wrong ecies header")

        #  1) generate shared-secret = kdf( ecdhAgree(myPrivKey, msg[1:65]) )
        _shared = data[1:1 + 64]
        # FIXME, check that _shared_pub is a valid one (on curve)

        key_material = self.raw_get_ecdh_key(
            pubkey_x=_shared[:32], pubkey_y=_shared[32:]
        )
        assert len(key_material) == 32
        key = eciesKDF(key_material, 32)
        assert len(key) == 32
        key_enc, key_mac = key[:16], key[16:]

        key_mac = hashlib.sha256(key_mac).digest()
        assert len(key_mac) == 32

        tag = data[-32:]
        assert len(tag) == 32

        # 2) verify tag
        hmaced_data = pyelliptic.hmac_sha256(
            key_mac, data[1 + 64:- 32] + shared_mac_data
        )
        if not pyelliptic.equals(hmaced_data, tag):
            raise exceptions.DecryptionError("Fail to verify data")

        # 3) decrypt
        blocksize = pyelliptic.OpenSSL.get_cipher(self.ecies_ciphername).get_blocksize()  # noqa
        iv = data[1 + 64:1 + 64 + blocksize]
        assert len(iv) == 16
        ciphertext = data[1 + 64 + blocksize:- 32]
        assert 1 + len(_shared) + len(iv) + len(ciphertext) + len(tag) == len(data)  # noqa
        ctx = pyelliptic.Cipher(key_enc, iv, 0, self.ecies_ciphername)
        return ctx.ciphering(ciphertext)
    encrypt = ecies_encrypt
    decrypt = ecies_decrypt

    def sign(self, inputb):
        signature = ecdsa_sign(self.raw_privkey, inputb)
        assert len(signature) == 65
        return signature

    def verify(self, sig, inputb):
        if len(sig) != 65:
            raise exceptions.InvalidSignature('Invalid length')
        return ecdsa_verify(self.raw_pubkey, sig, inputb)
