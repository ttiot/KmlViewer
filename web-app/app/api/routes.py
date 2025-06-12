"""
Routes API REST de l'application.
"""

from flask import request, jsonify
from app.api import bp
from app.services.kml_parser import KMLParser
from app.services.gpx_parser import GPXParser
from app.services.cache_service import parse_kml_cached, parse_gpx_cached
from app.services.file_service import FileService
from app.services.timing_tools import track_time

@bp.route('/upload', methods=['POST'])
@track_time
def upload_file():
    """Endpoint pour uploader et traiter un fichier KML ou GPX."""
    """Endpoint pour uploader et traiter un fichier KML ou GPX."""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'Aucun fichier sélectionné'}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'Aucun fichier sélectionné'}), 400
        
        # Récupérer le mode d'affichage depuis les paramètres
        display_mode = request.form.get('display_mode', 'double')
        
        # Traiter le fichier avec détection automatique du format
        content = FileService.save_uploaded_file(file)
        ext = file.filename.rsplit('.', 1)[1].lower()
        if ext == 'gpx':
            result = parse_gpx_cached(content)
        else:
            result = parse_kml_cached(content, display_mode)
        
        if result['success']:
            return jsonify(result)
        else:
            return jsonify(result), 400
            
    except ValueError as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 400
    except (IOError, OSError) as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors du traitement du fichier: {str(e)}'
        }), 500


@bp.route('/multi-upload', methods=['POST'])
@track_time
def multi_upload():
    """Endpoint pour uploader plusieurs fichiers en une seule requête."""
    try:
        if 'files' not in request.files:
            return jsonify({'success': False, 'error': 'Aucun fichier fourni'}), 400

        files = request.files.getlist('files')
        if not files:
            return jsonify({'success': False, 'error': 'Aucun fichier fourni'}), 400

        display_mode = request.form.get('display_mode', 'double')
        results = []
        for file in files:
            if not file or file.filename == '':
                results.append({'filename': '', 'success': False, 'error': 'Nom de fichier invalide'})
                continue

            if not FileService.allowed_file(file.filename):
                results.append({'filename': file.filename, 'success': False, 'error': 'Type de fichier non autorisé'})
                continue

            try:
                content = file.read().decode('utf-8')
            except UnicodeDecodeError:
                results.append({'filename': file.filename, 'success': False, 'error': "Erreur d'encodage"})
                continue

            ext = file.filename.rsplit('.', 1)[1].lower()
            if ext == 'gpx':
                res = parse_gpx_cached(content)
            else:
                res = parse_kml_cached(content, display_mode)
            res['filename'] = file.filename
            results.append(res)

        return jsonify({'files': results})

    except Exception as e:  # noqa: BLE001
        return jsonify({'success': False, 'error': f'Erreur : {str(e)}'}), 500


@bp.route('/sample-files')
def list_sample_files():
    """Liste les fichiers d'exemple disponibles."""
    try:
        sample_files = FileService.get_sample_files()
        return jsonify({'files': sample_files})
    except (IOError, OSError) as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors du chargement des fichiers d\'exemple: {str(e)}'
        }), 500


@bp.route('/load-sample/<filename>')
def load_sample_file(filename):
    """Charge un fichier d'exemple (KML ou GPX)."""
    """Charge un fichier d'exemple (KML ou GPX)."""
    try:
        # Récupérer le mode d'affichage depuis les paramètres de requête
        display_mode = request.args.get('display_mode', 'double')
        
        # Charger le fichier avec détection automatique du format
        content = FileService.load_sample_file(filename)
        ext = filename.rsplit('.', 1)[1].lower()
        if ext == 'gpx':
            result = parse_gpx_cached(content)
        else:
            result = parse_kml_cached(content, display_mode)
        
        return jsonify(result)
        
    except FileNotFoundError:
        return jsonify({
            'success': False,
            'error': 'Fichier non trouvé ou type invalide'
        }), 404
    except (IOError, OSError, UnicodeDecodeError) as e:
        return jsonify({
            'success': False,
            'error': f'Erreur lors du chargement du fichier: {str(e)}'
        }), 500


@bp.route('/health')
def health_check():
    """Endpoint de vérification de santé de l'API."""
    return jsonify({
        'status': 'healthy',
        'version': '2.0.0',
        'api': 'KML/GPX Viewer API'
    })

@bp.route('/stats')
def stats():
    """Route pour afficher les statistiques des durées."""
    from app.services.timing_tools import execution_times
    stats_data = {}
    for func_name, data in execution_times.items():
        avg_time = data['total_time'] / data['count'] if data['count'] > 0 else 0
        stats_data[func_name] = {
            'call_count': data['count'],
            'total_time': round(data['total_time'], 2),
            'average_time': round(avg_time, 2)
        }
    return jsonify(stats_data)