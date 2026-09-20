"""Routes for the task API.

A ``DefaultRouter`` generates the six CRUD routes from one registration and,
critically, generates them from the *same* viewset object that the code uses --
so the URL patterns cannot disagree with the view's declared actions. The API
root at ``/api/`` is the browsable index, available in DEBUG only.
"""
from rest_framework.routers import DefaultRouter

from .views import TaskViewSet

router = DefaultRouter()
router.register("tasks", TaskViewSet, basename="task")

urlpatterns = router.urls
