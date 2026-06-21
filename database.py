import os
from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, Time, Float, ForeignKey
from sqlalchemy.orm import sessionmaker, declarative_base, relationship

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./recordio.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Business(Base):
    __tablename__ = "businesses"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True)
    password_hash = Column(String, nullable=False)
    phone = Column(String)
    whatsapp_connected = Column(Boolean, default=False)
    timezone = Column(String, default="America/Argentina/Buenos_Aires")
    reminder_24h = Column(Boolean, default=True)
    reminder_1h = Column(Boolean, default=True)
    reminder_before_minutes = Column(Integer, default=0)
    message_template = Column(String, default="Hola {client}! Te recordamos tu turno de {service} el {date} a las {time}. Respondé CONFIRMAR para confirmar o CANCELAR para cancelar.")
    slug = Column(String, unique=True, nullable=True)
    active = Column(Boolean, default=True)
    plan = Column(String, default="free")
    stripe_customer_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    services = relationship("Service", back_populates="business", cascade="all, delete-orphan")
    availabilities = relationship("Availability", back_populates="business", cascade="all, delete-orphan")
    appointments = relationship("Appointment", back_populates="business", cascade="all, delete-orphan")


class Service(Base):
    __tablename__ = "services"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    name = Column(String, nullable=False)
    duration_minutes = Column(Integer, default=30)
    price = Column(Float, nullable=True)

    business = relationship("Business", back_populates="services")


class Availability(Base):
    __tablename__ = "availabilities"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    day_of_week = Column(Integer, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)

    business = relationship("Business", back_populates="availabilities")


class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True)
    business_id = Column(Integer, ForeignKey("businesses.id"), nullable=False)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=True)
    client_name = Column(String, nullable=False)
    client_phone = Column(String, nullable=False)
    date = Column(String, nullable=False)
    time = Column(String, nullable=False)
    status = Column(String, default="pending")
    reminder_24h_sent = Column(Boolean, default=False)
    reminder_1h_sent = Column(Boolean, default=False)
    reminder_before_sent = Column(Boolean, default=False)
    confirmed_at = Column(DateTime, nullable=True)
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    business = relationship("Business", back_populates="appointments")
    service = relationship("Service")


Base.metadata.create_all(bind=engine)
