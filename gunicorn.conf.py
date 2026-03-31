timeout = 300
workers = 1
```

И в `Render` в поле **Start Command** поставь:
```
gunicorn app:app --config gunicorn.conf.py
```

Или просто в Start Command:
```
gunicorn app:app --timeout 300 --workers 1