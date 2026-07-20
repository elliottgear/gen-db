#!/usr/bin/env python3
"""
Insert one batch of demo customers/generators into the Watt Watch database.

Adds BATCH_SIZE fake customers (a realistic Maine-themed mix of
individuals, couples, and property-management companies), each with
1-5 generators. Writes directly to whatever DB_PATH points at, so it
works locally or in the Railway environment (e.g. `railway run
python3 seed_batch.py` against the mounted volume).

Re-runnable: run it again for another batch of 50.

Usage:  python3 seed_batch.py
"""
import datetime
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import DB_PATH, get_conn, init_db  # noqa: E402

BATCH_SIZE = 50
CREATED_BY = 'seed-script'

FIRST_NAMES = [
    'Alexander', 'Amanda', 'Amy', 'Andrew', 'Angela', 'Anna', 'Anthony', 'Ashley',
    'Barbara', 'Benjamin', 'Betty', 'Brandon', 'Brian', 'Carol', 'Carolyn', 'Catherine',
    'Christine', 'Christopher', 'Cynthia', 'Daniel', 'David', 'Deborah', 'Debra', 'Dennis',
    'Donald', 'Donna', 'Dorothy', 'Edward', 'Elizabeth', 'Emily', 'Emma', 'Eric',
    'Frank', 'George', 'Gregory', 'Helen', 'Jack', 'Jacob', 'Janet', 'Jason',
    'Jeffrey', 'Jennifer', 'Jerry', 'Jessica', 'John', 'Jonathan', 'Joseph', 'Joshua',
    'Karen', 'Katherine', 'Kenneth', 'Kevin', 'Kimberly', 'Larry', 'Laura', 'Linda',
    'Lisa', 'Margaret', 'Maria', 'Mark', 'Mary', 'Matthew', 'Melissa', 'Michael',
    'Michelle', 'Nancy', 'Nicholas', 'Nicole', 'Pamela', 'Patricia', 'Patrick', 'Paul',
    'Rachel', 'Raymond', 'Richard', 'Robert', 'Ronald', 'Ruth', 'Ryan', 'Samuel',
    'Sarah', 'Scott', 'Sharon', 'Shirley', 'Stephanie', 'Stephen', 'Steven', 'Susan',
    'Thomas', 'Timothy', 'William',
]

LAST_NAMES = [
    'Allen', 'Anderson', 'Baker', 'Beaulieu', 'Belanger', 'Bergeron', 'Boucher',
    'Brown', 'Campbell', 'Chabot', 'Clark', 'Cormier', 'Cote', 'Dube', 'Fournier',
    'Gagne', 'Gagnon', 'Garcia', 'Gonzalez', 'Green', 'Hall', 'Harris', 'Hernandez',
    'Hill', 'Jackson', 'Johnson', 'Jones', 'King', 'Landry', 'Lee', 'Levesque',
    'Lewis', 'Lopez', 'Martin', 'Martinez', 'Michaud', 'Miller', 'Mitchell', 'Moore',
    'Morin', 'Nelson', 'Nguyen', 'Ouellette', 'Pelletier', 'Perez', 'Poulin', 'Ramirez',
    'Roberts', 'Robinson', 'Rodriguez', 'Roy', 'Sanchez', 'Scott', 'Smith', 'Taylor',
    'Theriault', 'Thomas', 'Thompson', 'Torres', 'Walker', 'White', 'Williams', 'Wilson',
    'Wright', 'Young',
]

ME_CITIES = [
    ('Portland', '04101'), ('Falmouth', '04105'), ('Cape Elizabeth', '04107'),
    ('Auburn', '04210'), ('Bath', '04530'), ('Kittery', '03904'), ('Bangor', '04401'),
    ('Brunswick', '04011'), ('Westbrook', '04092'), ('South Portland', '04106'),
    ('Saco', '04072'), ('Biddeford', '04005'), ('Scarborough', '04074'),
    ('Yarmouth', '04096'), ('Freeport', '04032'), ('Augusta', '04330'),
    ('Lewiston', '04240'), ('Gorham', '04038'), ('Windham', '04062'),
]

STREET_WORDS = [
    'Birch', 'Tidewater', 'Stonehedge', 'Quarry Ridge', 'Foxfield', 'Chandler',
    'Ocean', 'Cedarbrook', 'Elm Street', 'Sawyer Mill', 'Granite Hill', 'Harborview',
    'Pinecrest', 'Riverbend', 'Maplewood', 'Cumberland', 'Blue Heron', 'Northshore',
    'Highland', 'Sunrise', 'Bayview', 'Casco Bay', 'Foreside', 'Coastal',
]
STREET_SUFFIXES = ['Rd', 'Ln', 'St', 'Ave', 'Way', 'Dr', 'Ct']

COMPANY_SUFFIXES = [
    'Property Management', 'Property Solutions', 'Property Holdings', 'Rentals',
    'Realty Group', 'Housing Partners', 'Real Estate Services',
]

MAKE_MODELS = {
    'Generac': (['Guardian 22kW', 'Guardian 24kW', 'Protector 48kW'], 'GEN'),
    'Kohler': (['14RESAL', '20RESCL', '38RCLC'], 'KOH'),
    'Cummins': (['RS20A', 'RS30A', 'QuietConnect 22kW'], 'CUM'),
    'Briggs & Stratton': (['20kW Standby', '17kW Standby', 'Fortress 10kW'], 'BNS'),
    'Champion': (['14kW Home Standby', '12.5kW Home Standby'], 'CHP'),
    'Honeywell': (['16kW Standby', '20kW Standby'], 'HON'),
    'Deere': (['9kW Standby', '13kW Standby'], 'DEE'),
}

INSTALLERS = [
    'Coastal Power Solutions', 'Downeast Generator Co.', 'Pine Tree Standby Power',
    'Kennebec Electric & Generator', 'Northeast Backup Power',
]

CUSTOMER_NOTES = [
    '', '', '', '', 'Prefers morning appointments.', 'Dog on property (friendly).',
    'Call ahead before arriving.', 'Gate code required for access.',
]
GENERATOR_NOTES = [
    '', '', '', '', 'Runs on propane.', 'Natural gas fed.',
    'Access via rear alley gate.', 'Tank buried east side of house.',
]


def make_street():
    line = f'{random.randint(1, 999)} {random.choice(STREET_WORDS)} {random.choice(STREET_SUFFIXES)}'
    if random.random() < 0.15:
        line += f'\n{random.choice(["Apartment", "Unit", "Suite"])} {random.randint(1, 12)}'
    return line


def make_address():
    street = make_street()
    city, zipcode = random.choice(ME_CITIES)
    return f'{street}\n{city}, ME {zipcode}'


def make_customer():
    is_company = random.random() < 0.15
    if is_company:
        name = f'{random.choice(STREET_WORDS)} {random.choice(COMPANY_SUFFIXES)}'
        first_for_email = name.split()[0]
    elif random.random() < 0.35:
        first_a, first_b = random.sample(FIRST_NAMES, 2)
        last = random.choice(LAST_NAMES)
        name = f'{first_a} & {first_b} {last}'
        first_for_email = first_a
    else:
        first_for_email = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        name = f'{first_for_email} {last}'

    last_for_email = name.split()[-1]
    email = f'{first_for_email.lower()}.{last_for_email.lower()}{random.randint(1, 99)}@example.com'
    phone = f'(207) 555-{random.randint(0, 9999):04d}'
    physical_address = make_address()
    billing_address = physical_address
    if not is_company and random.random() < 0.1:
        city, zipcode = random.choice(ME_CITIES)
        billing_address = f'PO Box {random.randint(100, 999)}\n{city}, ME {zipcode}'

    return dict(
        name=name, email=email, phone=phone,
        physical_address=physical_address, billing_address=billing_address,
        notes=random.choice(CUSTOMER_NOTES), is_company=is_company,
    )


def make_serial(make, used_serials):
    prefix = MAKE_MODELS[make][1]
    while True:
        serial = f'{prefix}-{random.randint(100000, 999999)}-{random.choice("ABCDEF")}'
        if serial not in used_serials:
            used_serials.add(serial)
            return serial


def make_service_dates(install_date, today):
    outcome = random.random()
    if outcome < 0.2:
        return '', ''  # no service on file
    years_since_install = max((today - install_date).days // 365, 0)
    last_service = install_date + datetime.timedelta(days=random.randint(180, 365 * max(years_since_install, 1)))
    if last_service > today:
        last_service = today - datetime.timedelta(days=random.randint(0, 60))
    if outcome < 0.55:
        next_service = last_service + datetime.timedelta(days=365)
    elif outcome < 0.75:
        next_service = today + datetime.timedelta(days=random.randint(1, 60))  # due soon
    else:
        next_service = today - datetime.timedelta(days=random.randint(1, 120))  # overdue
    return last_service.isoformat(), next_service.isoformat()


def make_generator(customer, used_serials, today):
    make = random.choice(list(MAKE_MODELS.keys()))
    model = random.choice(MAKE_MODELS[make][0])
    serial = make_serial(make, used_serials)
    if customer['is_company'] and random.random() < 0.4:
        physical_address = make_address()
    else:
        physical_address = customer['physical_address']
    install_date = today - datetime.timedelta(days=random.randint(180, 365 * 8))
    last_service, next_service = make_service_dates(install_date, today)
    return dict(
        make=make, model=model, serial=serial, physical_address=physical_address,
        install_date=install_date.isoformat(), last_service_date=last_service,
        next_service_date=next_service, installer_name=random.choice(INSTALLERS),
        notes=random.choice(GENERATOR_NOTES),
    )


def insert_batch(conn):
    today = datetime.date.today()
    used_serials = set()
    generator_count = 0

    for _ in range(BATCH_SIZE):
        c = make_customer()
        cur = conn.execute(
            '''INSERT INTO customers (name,email,phone,physical_address,billing_address,notes,created_by,updated_by)
               VALUES (?,?,?,?,?,?,?,?)''',
            (c['name'], c['email'], c['phone'], c['physical_address'], c['billing_address'],
             c['notes'], CREATED_BY, CREATED_BY)
        )
        customer_id = cur.lastrowid

        for _ in range(random.randint(1, 5)):
            g = make_generator(c, used_serials, today)
            cur = conn.execute(
                '''INSERT INTO generators
                   (customer_id,make,model,serial,physical_address,install_date,last_service_date,
                    next_service_date,installer_name,notes,created_by,updated_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (customer_id, g['make'], g['model'], g['serial'], g['physical_address'],
                 g['install_date'], g['last_service_date'], g['next_service_date'],
                 g['installer_name'], g['notes'], CREATED_BY, CREATED_BY)
            )
            gen_id = cur.lastrowid
            conn.execute(
                '''INSERT INTO generator_history (generator_id,date,event,detail,created_by)
                   VALUES (?,?,?,?,?)''',
                (gen_id, g['install_date'], 'Installed',
                 f"Installed for {c['name']} by {g['installer_name']}.", CREATED_BY)
            )
            generator_count += 1

    conn.commit()
    return generator_count


def main():
    init_db()
    conn = get_conn()
    try:
        generator_count = insert_batch(conn)
    finally:
        conn.close()
    print(f'Inserted {BATCH_SIZE} customers and {generator_count} generators.')
    print(f'Database: {DB_PATH}')


if __name__ == '__main__':
    main()
