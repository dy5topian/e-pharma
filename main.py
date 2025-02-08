from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, Dict
import uuid
import stripe
from datetime import datetime
import os
from enum import Enum

from models import PaymentStatus, PaymentModel
from database import SessionLocal, engine, Base

# Create database tables
Base.metadata.create_all(bind=engine)

# Initialize Stripe
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

class PaymentRequest(BaseModel):
    amount: float
    currency: str
    order_id: str
    payment_method: str
    customer_id: Optional[str] = None
    metadata: Optional[Dict] = None
    return_url: str  # URL to redirect after payment
    cancel_url: str  # URL to redirect if payment is cancelled

class PaymentResponse(BaseModel):
    payment_id: str
    status: PaymentStatus
    amount: float
    currency: str
    created_at: str
    order_id: str
    checkout_url: Optional[str] = None  # Stripe checkout URL

class Payment(BaseModel):
    payment_id: str
    status: PaymentStatus
    amount: float
    currency: str
    order_id: str
    customer_id: Optional[str]
    payment_method: str
    created_at: str
    metadata: Optional[Dict]
    stripe_payment_intent_id: Optional[str] = None
    stripe_session_id: Optional[str] = None

# API security
api_key_header = APIKeyHeader(name="X-API-Key")

app = FastAPI(title="Payment Microservice")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify your allowed origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dependency for database sessions
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

async def verify_api_key(api_key: str = Depends(api_key_header)):
    if api_key != os.getenv("API_KEY"):
        raise HTTPException(
            status_code=403,
            detail="Invalid API key"
        )
    return api_key

@app.post("/api/v1/payments", response_model=PaymentResponse)
async def create_payment(
    payment_request: PaymentRequest,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    payment_id = str(uuid.uuid4())
    stripe_amount = int(payment_request.amount * 100)
    
    try:
        # Create Stripe Checkout Session
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': payment_request.currency.lower(),
                    'unit_amount': stripe_amount,
                    'product_data': {
                        'name': f'Order {payment_request.order_id}',
                    },
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=payment_request.return_url,
            cancel_url=payment_request.cancel_url,
            metadata={
                'payment_id': payment_id,
                'order_id': payment_request.order_id,
            }
        )
        
        # Create database record
        db_payment = PaymentModel(
            payment_id=payment_id,
            status=PaymentStatus.PENDING,
            amount=payment_request.amount,
            currency=payment_request.currency,
            order_id=payment_request.order_id,
            customer_id=payment_request.customer_id,
            payment_method=payment_request.payment_method,
            metadata=payment_request.metadata,
            stripe_session_id=checkout_session.id
        )
        
        db.add(db_payment)
        db.commit()
        db.refresh(db_payment)
        
        return PaymentResponse(
            payment_id=db_payment.payment_id,
            status=db_payment.status,
            amount=db_payment.amount,
            currency=db_payment.currency,
            created_at=db_payment.created_at.isoformat(),
            order_id=db_payment.order_id,
            checkout_url=checkout_session.url
        )
        
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/v1/payments/{payment_id}", response_model=Payment)
async def get_payment(
    payment_id: str,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    payment = db.query(PaymentModel).filter(PaymentModel.payment_id == payment_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    if payment.stripe_session_id:
        # Update payment status from Stripe
        session = stripe.checkout.Session.retrieve(payment.stripe_session_id)
        if session.payment_status == 'paid' and payment.status != PaymentStatus.CONFIRMED:
            payment.status = PaymentStatus.CONFIRMED
            db.commit()
        elif session.status == 'expired' and payment.status == PaymentStatus.PENDING:
            payment.status = PaymentStatus.FAILED
            db.commit()
    
    return payment

@app.post("/api/v1/payments/{payment_id}/refund")
async def refund_payment(
    payment_id: str,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    payment = db.query(PaymentModel).filter(PaymentModel.payment_id == payment_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    if payment.status != PaymentStatus.CONFIRMED:
        raise HTTPException(status_code=400, detail="Payment cannot be refunded")
    
    try:
        session = stripe.checkout.Session.retrieve(payment.stripe_session_id)
        refund = stripe.Refund.create(payment_intent=session.payment_intent)
        
        if refund.status == 'succeeded':
            payment.status = PaymentStatus.REFUNDED
            db.commit()
            return {"message": "Payment refunded successfully"}
        else:
            raise HTTPException(status_code=400, detail="Refund failed")
            
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/v1/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, WEBHOOK_SECRET
        )
        
        if event.type == 'checkout.session.completed':
            session = event.data.object
            payment_id = session.metadata.get('payment_id')
            
            if payment_id:
                payment = db.query(PaymentModel).filter(
                    PaymentModel.payment_id == payment_id
                ).first()
                
                if payment:
                    payment.status = PaymentStatus.CONFIRMED
                    payment.stripe_payment_intent_id = session.payment_intent
                    db.commit()
                
        return {"status": "success"}
        
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        raise HTTPException(status_code=400, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
