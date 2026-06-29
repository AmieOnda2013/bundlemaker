import os
import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id             = db.Column(db.Integer, primary_key=True)
    email          = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash  = db.Column(db.String(255), nullable=False)
    created_at     = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    is_active      = db.Column(db.Boolean, default=True, nullable=False)

    # Subscription fields (used in Phase 2)
    plan           = db.Column(db.String(50), default="free")   # free | solo | firm
    bundles_used   = db.Column(db.Integer, default=0)
    stripe_customer_id     = db.Column(db.String(255), nullable=True)
    stripe_subscription_id = db.Column(db.String(255), nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password, method="pbkdf2:sha256:600000")

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def can_generate(self):
        """Free plan: 3 bundles max. Paid plans: unlimited."""
        if self.plan != "free":
            return True
        return self.bundles_used < 3

    def __repr__(self):
        return f"<User {self.email}>"
