import datetime
import secrets
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

PLAN_LIMITS = {
    "free":         3,   # lifetime total
    "solo":         20,  # per month
    "professional": 60,  # per month
    "firm":         None # unlimited
}


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True)
    email         = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at    = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    is_active     = db.Column(db.Boolean, default=True, nullable=False)

    # Email verification
    email_verified       = db.Column(db.Boolean, default=False, nullable=False)
    email_verify_token   = db.Column(db.String(128), nullable=True)

    # Subscription
    plan                 = db.Column(db.String(50), default="free")  # free | solo | professional | firm
    plan_period          = db.Column(db.String(20), default="monthly")  # monthly | annual
    bundles_used         = db.Column(db.Integer, default=0)   # lifetime (free) or current month (paid)
    bundles_reset_date   = db.Column(db.DateTime, nullable=True)  # when monthly counter resets
    stripe_customer_id      = db.Column(db.String(255), nullable=True)
    stripe_subscription_id  = db.Column(db.String(255), nullable=True)
    password_reset_token    = db.Column(db.String(128), nullable=True)
    password_reset_expires  = db.Column(db.DateTime, nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password, method="pbkdf2:sha256:600000")

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def generate_verify_token(self):
        self.email_verify_token = secrets.token_urlsafe(32)
        return self.email_verify_token

    def generate_reset_token(self):
        import datetime
        self.password_reset_token   = secrets.token_urlsafe(32)
        self.password_reset_expires = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
        return self.password_reset_token

    def reset_monthly_bundles_if_due(self):
        """Reset monthly bundle counter if a new billing month has started."""
        if self.plan == "free":
            return
        now = datetime.datetime.utcnow()
        if self.bundles_reset_date is None or now >= self.bundles_reset_date:
            self.bundles_used = 0
            self.bundles_reset_date = now + datetime.timedelta(days=30)

    def can_generate(self):
        """Check if user is allowed to generate a bundle."""
        if not self.email_verified:
            return False
        if self.plan == "free":
            return self.bundles_used < 3
        if self.plan == "firm":
            return True
        limit = PLAN_LIMITS.get(self.plan, 0)
        return self.bundles_used < limit

    def bundles_remaining(self):
        if self.plan == "firm":
            return None  # unlimited
        if self.plan == "free":
            return max(0, 3 - self.bundles_used)
        limit = PLAN_LIMITS.get(self.plan, 0)
        return max(0, limit - self.bundles_used)

    def __repr__(self):
        return f"<User {self.email}>"
