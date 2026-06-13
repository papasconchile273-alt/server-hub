import requests
import os

def publicar_en_facebook(texto_post, token_acceso):
    if not token_acceso:
        print("Error: falta FB_ACCESS_TOKEN (variable de entorno)")
        return False, "missing_token"
    # Usamos el Page ID directamente en vez de 'me'
    # 'me/feed' a veces falla con tokens de página
    url = "https://graph.facebook.com/v20.0/me/feed"
    
    payload = {
        'message': texto_post,
        'access_token': token_acceso
    }
    
    try:
        response = requests.post(url, data=payload, timeout=10)
        response.raise_for_status()  # Truena si hay error HTTP
        resultado = response.json()
        
        if 'id' in resultado:
            print(f"¡Éxito pa! Publicado con ID: {resultado['id']}")
            return True, resultado['id']
        else:
            print("Error de Meta:", resultado)
            return False, resultado
            
    except requests.exceptions.Timeout:
        print("Error: El servidor tardó mucho en responder")
        return False, "timeout"
    except requests.exceptions.HTTPError as e:
        print(f"Error HTTP: {e.response.status_code} - {e.response.text}")
        return False, str(e)
    except Exception as e:
        print("Error inesperado:", str(e))
        return False, str(e)


# Token SIEMPRE desde variable de entorno, nunca hardcodeado
MI_TOKEN = os.environ.get("FB_ACCESS_TOKEN")

# Prueba manual (no se ejecuta al importar el módulo)
if __name__ == "__main__":
    publicar_en_facebook("¡Primer posteo automático desde Server Hub! 🚀", MI_TOKEN)