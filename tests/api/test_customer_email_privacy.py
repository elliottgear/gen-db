"""Customer email addresses: admins see them; technicians see j•••@domain and can't change them (v0.9.0)."""
import pytest

import core
import server


@pytest.mark.parametrize('raw, masked', [
    ('jane.doe@gmail.com', 'j•••@gmail.com'),
    ('j@x.org', 'j•••@x.org'),
    ('Pat+Generator@Example.COM', 'P•••@Example.COM'),
    ('  pat@example.com  ', 'p•••@example.com'),
    ('a@b@c.com', 'a•••@c.com'),          # domain is what follows the last @
    ('not-an-email', '•••'),
    ('@example.com', '•••'),
    ('pat@', '•••'),
    ('', ''),
    (None, ''),
])
def test_mask_email(raw, masked):
    assert core.mask_email(raw) == masked


def test_only_admins_see_emails():
    assert [role for role, perms in server.ROLES.items() if 'customers.view_email' in perms] == ['admin']


@pytest.fixture
def customer(admin):
    status, c = admin.json('POST', '/api/customers', {'name': 'Jane Doe', 'email': 'jane.doe@gmail.com',
                                                      'phone': '2075550142'})
    assert status == 200 and c['email'] == 'jane.doe@gmail.com' and 'emailHidden' not in c
    return c


def test_technicians_get_masked_emails_everywhere(tech, customer):
    res, raw = tech.request('GET', '/api/customers')
    assert b'jane.doe' not in raw
    listed = next(c for c in tech.json('GET', '/api/customers')[1] if c['id'] == customer['id'])
    one = tech.json('GET', f"/api/customers/{customer['id']}")[1]
    for c in (listed, one):
        assert (c['email'], c['emailHidden']) == ('j•••@gmail.com', True)
        assert c['phone'] == '2075550142'  # only the email is hidden


def test_admins_get_the_full_email(admin, customer):
    for c in (admin.json('GET', f"/api/customers/{customer['id']}")[1],
              next(c for c in admin.json('GET', '/api/customers')[1] if c['id'] == customer['id'])):
        assert c['email'] == 'jane.doe@gmail.com' and 'emailHidden' not in c


@pytest.mark.parametrize('sent', ['someone.else@evil.example', 'j•••@gmail.com', '', None])
def test_a_technician_edit_never_changes_the_email(tech, admin, customer, sent):
    body = {'name': 'Jane Q. Doe', 'phone': '2075559999'}
    if sent is not None:
        body['email'] = sent
    status, out = tech.json('PUT', f"/api/customers/{customer['id']}", body)
    assert status == 200 and out['email'] == 'j•••@gmail.com'
    stored = admin.json('GET', f"/api/customers/{customer['id']}")[1]
    assert (stored['name'], stored['phone'], stored['email']) == ('Jane Q. Doe', '2075559999', 'jane.doe@gmail.com')


def test_a_technician_can_add_an_email_to_a_new_customer(tech, admin):
    status, out = tech.json('POST', '/api/customers', {'name': 'New Person', 'email': 'new.person@example.com'})
    assert status == 200 and (out['email'], out['emailHidden']) == ('n•••@example.com', True)
    assert admin.json('GET', f"/api/customers/{out['id']}")[1]['email'] == 'new.person@example.com'


def test_admins_can_still_change_emails(admin, customer):
    status, out = admin.json('PUT', f"/api/customers/{customer['id']}", {'name': 'Jane Doe', 'email': 'jd@new.example'})
    assert status == 200 and out['email'] == 'jd@new.example'


def test_the_page_locks_the_email_field_when_it_is_hidden():
    page = open(server.STATIC_FILE).read()
    assert 'Only admins can see or change email addresses.' in page
    assert 'if(!emailLocked) data.email = f.email.value.trim();' in page
    assert 'editing && editing.emailHidden' in page
