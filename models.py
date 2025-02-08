from sqlalchemy import Column, String, Float, DateTime, Enum as SQLAEnum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime
from enum import Enum as PyEnum

Base = declarative_base()

class PaymentStatus(str, PyEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"

class PaymentModel(Base):
    __tablename__ = "payments"

    payment_id = Column(String, primary_key=True)
    status = Column(SQLAEnum(PaymentStatus), nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String, nullable=False)
    order_id = Column(String, nullable=False)
    customer_id = Column(String, nullable=True)
    payment_method = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    metadata = Column(JSONB, nullable=True)
    stripe_payment_intent_id = Column(String, nullable=True)
    stripe_session_id = Column(String, nullable=True)
