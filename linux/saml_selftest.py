#!/usr/bin/env python3
"""SAML self-test: acts as the IdP, signs a real SAML Response with xmlsec, and proves saml.acs() accepts a valid
assertion and REJECTS tampered / wrong-cert / expired / bad-InResponseTo / unsigned / double-assertion ones.
Run on the server (needs python3-xmlsec, python3-lxml, cryptography). Uses a throwaway DB; touches nothing in production.
SPDX-License-Identifier: AGPL-3.0-or-later
    cd /opt/pxe-toolkit && python3 linux/saml_selftest.py
"""
import base64
import datetime
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # /opt/pxe-toolkit
import db
import saml
import xmlsec
from lxml import etree
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ACS = "https://kb.test:9443/sso/saml/acs"
IDP = "https://idp.test/metadata"
SP = "kwickboot"
NSA = "urn:oasis:names:tc:SAML:2.0:assertion"
NSP = "urn:oasis:names:tc:SAML:2.0:protocol"


def gen_cert():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "saml-selftest-idp")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption())
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return key_pem, cert_pem


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_response(rid, key_pem, cert_pem, sign=True, two_assertions=False, not_on_or_after=None, aid="_a1"):
    now = datetime.datetime.utcnow()
    noa = not_on_or_after or iso(now + datetime.timedelta(minutes=5))
    nb = iso(now - datetime.timedelta(minutes=5))
    def assertion(_id):
        return (f'<saml:Assertion xmlns:saml="{NSA}" ID="{_id}" Version="2.0" IssueInstant="{iso(now)}">'
                f'<saml:Issuer>{IDP}</saml:Issuer>'
                f'<saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:2.0:nameid-format:persistent">alice@school.test</saml:NameID>'
                f'<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
                f'<saml:SubjectConfirmationData Recipient="{ACS}" NotOnOrAfter="{noa}" InResponseTo="{rid}"/>'
                f'</saml:SubjectConfirmation></saml:Subject>'
                f'<saml:Conditions NotBefore="{nb}" NotOnOrAfter="{noa}">'
                f'<saml:AudienceRestriction><saml:Audience>{SP}</saml:Audience></saml:AudienceRestriction></saml:Conditions>'
                f'<saml:AttributeStatement><saml:Attribute Name="groups">'
                f'<saml:AttributeValue>staff</saml:AttributeValue></saml:Attribute>'
                f'<saml:Attribute Name="email"><saml:AttributeValue>alice@school.test</saml:AttributeValue></saml:Attribute>'
                f'</saml:AttributeStatement></saml:Assertion>')
    body = assertion(aid) + (assertion("_a2") if two_assertions else "")
    resp = (f'<samlp:Response xmlns:samlp="{NSP}" xmlns:saml="{NSA}" ID="_r1" Version="2.0" '
            f'IssueInstant="{iso(now)}" InResponseTo="{rid}" Destination="{ACS}">'
            f'<saml:Issuer>{IDP}</saml:Issuer>'
            f'<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
            f'{body}</samlp:Response>')
    root = etree.fromstring(resp.encode())
    if sign:
        signed = root.find(f"{{{NSA}}}Assertion")
        xmlsec.tree.add_ids(signed, ["ID"])
        sig = xmlsec.template.create(signed, xmlsec.constants.TransformExclC14N, xmlsec.constants.TransformRsaSha256)
        issuer = signed.find(f"{{{NSA}}}Issuer")
        signed.insert(list(signed).index(issuer) + 1, sig)      # Signature right after Issuer, as SAML requires
        ref = xmlsec.template.add_reference(sig, xmlsec.constants.TransformSha256, uri="#" + aid)
        xmlsec.template.add_transform(ref, xmlsec.constants.TransformEnveloped)
        xmlsec.template.add_transform(ref, xmlsec.constants.TransformExclC14N)
        ki = xmlsec.template.ensure_key_info(sig)
        xmlsec.template.add_x509_data(ki)
        ctx = xmlsec.SignatureContext()
        k = xmlsec.Key.from_memory(key_pem, xmlsec.constants.KeyDataFormatPem)
        k.load_cert_from_memory(cert_pem, xmlsec.constants.KeyDataFormatCertPem)
        ctx.key = k
        ctx.sign(sig)
    return base64.b64encode(etree.tostring(root)).decode()


def expect_ok(name, rid, b64):
    saml.PENDING[rid] = {"provider": "selftest", "exp": time.time() + 600, "acs": ACS}
    try:
        prov, claims = saml.acs(b64, ACS)
        email, dname, role, depts = saml.resolve(prov, claims)
        assert role == "operator", f"role={role}"
        assert email == "alice@school.test", f"email={email}"
        print(f"  PASS  {name}: accepted, email={email} role={role}")
        return True
    except Exception as e:
        print(f"  FAIL  {name}: should have accepted but raised: {e}")
        return False


def expect_reject(name, rid, b64):
    saml.PENDING[rid] = {"provider": "selftest", "exp": time.time() + 600, "acs": ACS}
    try:
        saml.acs(b64, ACS)
        print(f"  FAIL  {name}: should have been REJECTED but was accepted")
        return False
    except saml.SAMLError as e:
        print(f"  PASS  {name}: rejected ({str(e)[:70]})")
        return True
    except Exception as e:
        print(f"  FAIL  {name}: wrong exception type: {type(e).__name__}: {e}")
        return False


def main():
    if not saml.HAVE_LIB:
        print("FAIL: python3-xmlsec / lxml not available"); sys.exit(1)
    tmp = tempfile.mkdtemp()
    db.init(os.path.join(tmp, "selftest.db"))
    try:
        db.init  # schema created by init
    except Exception:
        pass
    key_pem, cert_pem = gen_cert()
    _, other_cert = gen_cert()                 # a different cert (wrong key) for the wrong-cert test
    saml.save_provider({"id": "selftest", "name": "Self Test", "idp_entity_id": IDP, "idp_sso_url": "https://idp.test/sso",
                        "idp_x509": cert_pem.decode(), "sp_entity_id": SP, "domains": ["school.test"],
                        "group_attribute": "groups", "group_rules": [{"group": "staff", "role": "operator", "departments": []}]})
    ok = True
    print("SAML verification self-test:")
    ok &= expect_ok("valid signed assertion", "_req_valid", build_response("_req_valid", key_pem, cert_pem))

    # tamper: flip bytes in the signed doc
    good = base64.b64decode(build_response("_req_tamper", key_pem, cert_pem))
    bad = good.replace(b"alice@school.test", b"admin@school.test", 1)   # change signed content -> digest/sig must fail
    ok &= expect_reject("tampered assertion content", "_req_tamper", base64.b64encode(bad).decode())

    # wrong cert configured (signed by a key whose cert is not the one configured)
    saml.save_provider({"id": "selftest", "name": "Self Test", "idp_entity_id": IDP, "idp_sso_url": "https://idp.test/sso",
                        "idp_x509": other_cert.decode(), "sp_entity_id": SP, "domains": ["school.test"],
                        "group_attribute": "groups", "group_rules": [{"group": "staff", "role": "operator", "departments": []}]})
    ok &= expect_reject("signature vs wrong IdP cert", "_req_wrongcert", build_response("_req_wrongcert", key_pem, cert_pem))
    # restore correct cert
    saml.save_provider({"id": "selftest", "name": "Self Test", "idp_entity_id": IDP, "idp_sso_url": "https://idp.test/sso",
                        "idp_x509": cert_pem.decode(), "sp_entity_id": SP, "domains": ["school.test"],
                        "group_attribute": "groups", "group_rules": [{"group": "staff", "role": "operator", "departments": []}]})

    ok &= expect_reject("expired assertion", "_req_exp",
                        build_response("_req_exp", key_pem, cert_pem, not_on_or_after=iso(datetime.datetime.utcnow() - datetime.timedelta(minutes=10))))
    ok &= expect_reject("unsigned assertion", "_req_uns", build_response("_req_uns", key_pem, cert_pem, sign=False))
    ok &= expect_reject("two assertions (wrapping)", "_req_wrap", build_response("_req_wrap", key_pem, cert_pem, two_assertions=True))

    # bad InResponseTo: sign a response whose InResponseTo does not match the pending id we register
    b64 = build_response("_mismatch", key_pem, cert_pem)
    saml.PENDING["_req_bad_irt"] = {"provider": "selftest", "exp": time.time() + 600, "acs": ACS}
    try:
        saml.acs(b64, ACS)   # response InResponseTo=_mismatch, not a known pending id -> reject
        print("  FAIL  bad InResponseTo: accepted"); ok = False
    except saml.SAMLError as e:
        print(f"  PASS  bad InResponseTo: rejected ({str(e)[:60]})")

    saml.delete_provider("selftest")
    print("RESULT:", "ALL PASS" if ok else "FAILURES ABOVE")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
