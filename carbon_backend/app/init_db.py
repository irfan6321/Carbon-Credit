from app.database import engine, Base
from app.models import Project # Ensure your models are imported

print("Creating database tables...")
Base.metadata.create_all(bind=engine)
print("Database tables created.")