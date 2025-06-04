#!/usr/bin/env python3
"""
Application web Flask pour visualiser des fichiers KML sur une carte interactive.
Permet de choisir différents fonds de carte et d'uploader des fichiers KML.
"""

import os
from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename
from pathlib import Path

# Import des services et routes
from app.services.kml_parser import KMLParser
from app.api.analysis_routes import analysis_bp

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SECRET_KEY'] = 'your-secret-key-change-in-production'

# Créer le dossier d'upload s'il n'existe pas
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Enregistrer les blueprints
app.register_blueprint(analysis_bp)

ALLOWED_EXTENSIONS = {'kml', 'kmz'}

def allowed_file(filename):
    """Vérifie si le fichier a une extension autorisée."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Les fonctions de parsing ont été déplacées vers app.services.kml_parser

@app.route('/')
def index():
    """Page principale de l'application."""
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    """Endpoint pour uploader et traiter un fichier KML."""
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'Aucun fichier sélectionné'})
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'Aucun fichier sélectionné'})
    
    # Récupérer le mode d'affichage depuis les paramètres
    display_mode = request.form.get('display_mode', 'double')
    
    if file and allowed_file(file.filename):
        try:
            # Lire le contenu du fichier
            content = file.read().decode('utf-8')
            
            # Parser le KML avec le mode d'affichage
            result = KMLParser.parse_kml_coordinates(content, display_mode)
            
            if result['success']:
                return jsonify(result)
            else:
                return jsonify(result), 400
                
        except UnicodeDecodeError:
            return jsonify({
                'success': False,
                'error': 'Erreur d\'encodage du fichier. Assurez-vous qu\'il s\'agit d\'un fichier KML valide.'
            }), 400
        except (IOError, OSError) as e:
            return jsonify({
                'success': False,
                'error': f'Erreur lors du traitement du fichier: {str(e)}'
            }), 500
    else:
        return jsonify({
            'success': False,
            'error': 'Type de fichier non autorisé. Seuls les fichiers .kml sont acceptés.'
        }), 400

@app.route('/sample-files')
def list_sample_files():
    """Liste les fichiers KML d'exemple disponibles."""
    sample_files = []
    
    # Chercher dans le dossier sample_files monté par Docker
    sample_dir = Path('/app/sample_files')
    if sample_dir.exists():
        for file_path in sample_dir.glob('*.kml'):
            sample_files.append({
                'name': file_path.name,
                'path': str(file_path.name)
            })
    else:
        # Fallback pour le développement local
        project_root = Path(__file__).parent.parent
        if project_root.exists():
            for file_path in project_root.glob('*.kml'):
                sample_files.append({
                    'name': file_path.name,
                    'path': str(file_path.name)
                })
    
    return jsonify({'files': sample_files})

@app.route('/load-sample/<filename>')
def load_sample_file(filename):
    """Charge un fichier KML d'exemple."""
    try:
        secure_name = secure_filename(filename)
        
        # Récupérer le mode d'affichage depuis les paramètres de requête
        display_mode = request.args.get('display_mode', 'double')
        
        # Chercher d'abord dans le dossier sample_files monté par Docker
        sample_path = Path('/app/sample_files') / secure_name
        if sample_path.exists() and sample_path.suffix.lower() == '.kml':
            file_path = sample_path
        else:
            # Fallback pour le développement local
            project_root = Path(__file__).parent.parent
            file_path = project_root / secure_name
        
        if not file_path.exists() or not file_path.suffix.lower() == '.kml':
            return jsonify({
                'success': False,
                'error': 'Fichier non trouvé ou type invalide'
            }), 404
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        result = KMLParser.parse_kml_coordinates(content, display_mode)
        return jsonify(result)
        
    except (IOError, OSError, UnicodeDecodeError) as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors du chargement du fichier: {str(e)}'
        }), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)