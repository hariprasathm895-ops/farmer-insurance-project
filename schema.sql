CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    email TEXT,
    password TEXT
);

CREATE TABLE claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    crop TEXT,
    damage TEXT,
    date TEXT
);
