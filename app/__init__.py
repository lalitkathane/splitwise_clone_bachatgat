from flask import Flask
from app.extensions import db, login_manager
from config import Config


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # Initialize extensions
    db.init_app(app)
    login_manager.init_app(app)

    # User loader for Flask-Login
    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # Register blueprints
    from app.routes.auth import auth_bp
    from app.routes.groups import groups_bp
    from app.routes.loans import loans_bp
    from app.routes.wallet import wallet_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(groups_bp)
    app.register_blueprint(loans_bp)
    app.register_blueprint(wallet_bp)

    # ✅ CREATE ALL TABLES (Keep this for now!)
    with app.app_context():
        db.create_all()
        print("✅ Database tables created!")

    return app