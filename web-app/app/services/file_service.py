"""
Service de gestion des fichiers.
"""

from pathlib import Path
from typing import Dict, List
from werkzeug.utils import secure_filename
from flask import current_app


class FileService:
    """Service de gestion des fichiers KML."""
    
    @staticmethod
    def allowed_file(filename: str) -> bool:
        """Vérifie si le fichier a une extension autorisée."""
        return ('.' in filename and 
                filename.rsplit('.', 1)[1].lower() in current_app.config['ALLOWED_EXTENSIONS'])
    
    @staticmethod
    def get_sample_files() -> List[Dict[str, str]]:
        """Liste les fichiers KML d'exemple disponibles."""
        sample_files = []
        
        # Chercher dans le dossier sample_files monté par Docker
        sample_dir = Path(current_app.config['SAMPLE_FILES_DIR'])
        if sample_dir.exists():
            for file_path in sample_dir.glob('*.kml'):
                sample_files.append({
                    'name': file_path.name,
                    'path': str(file_path.name)
                })
        else:
            # Fallback pour le développement local
            project_root = Path(current_app.config['SAMPLE_FILES_FALLBACK'])
            if project_root.exists():
                for file_path in project_root.glob('*.kml'):
                    sample_files.append({
                        'name': file_path.name,
                        'path': str(file_path.name)
                    })
        
        return sample_files
    
    @staticmethod
    def load_sample_file(filename: str) -> str:
        """Charge le contenu d'un fichier KML d'exemple."""
        secure_name = secure_filename(filename)
        
        # Chercher d'abord dans le dossier sample_files monté par Docker
        sample_path = Path(current_app.config['SAMPLE_FILES_DIR']) / secure_name
        if sample_path.exists() and sample_path.suffix.lower() == '.kml':
            file_path = sample_path
        else:
            # Fallback pour le développement local
            project_root = Path(current_app.config['SAMPLE_FILES_FALLBACK'])
            file_path = project_root / secure_name
        
        if not file_path.exists() or not file_path.suffix.lower() == '.kml':
            raise FileNotFoundError('Fichier non trouvé ou type invalide')
        
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    
    @staticmethod
    def save_uploaded_file(file) -> str:
        """Sauvegarde un fichier uploadé et retourne son contenu."""
        if not file or not FileService.allowed_file(file.filename):
            raise ValueError('Type de fichier non autorisé')
        
        # Pour l'instant, on lit directement le contenu sans sauvegarder
        # Dans une version future, on pourrait sauvegarder le fichier
        try:
            content = file.read().decode('utf-8')
            return content
        except UnicodeDecodeError as exc:
            raise ValueError('Erreur d\'encodage du fichier. Assurez-vous qu\'il s\'agit d\'un fichier KML valide.') from exc