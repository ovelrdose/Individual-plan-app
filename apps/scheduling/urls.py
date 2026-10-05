from django.urls import path

from . import views

app_name = "scheduling"
urlpatterns = [
    path("board/", views.board_view, name="board"),
    path("board/cell/", views.board_cell, name="board_cell"),
    path("board/place/", views.board_place, name="board_place"),
    path("board/booking/<int:pk>/move/", views.board_move, name="board_move"),
    path("board/booking/<int:pk>/remove/", views.board_remove, name="board_remove"),
    path("board/block/", views.board_block, name="board_block"),
    path("board/block/clear/", views.board_block_clear, name="board_block_clear"),
    path("board/booking/<int:pk>/prefer/", views.board_prefer, name="board_prefer"),
    path("board/off/", views.board_off, name="board_off"),
    path("board/export/", views.board_export, name="board_export"),
    path("changes/", views.changes, name="changes"),
    path("load/", views.load, name="load"),
    path("changes/<int:pk>/", views.change, name="change"),
    path("booking/<int:pk>/", views.booking_panel, name="booking_panel"),
    path("booking/<int:pk>/edit/", views.booking_edit, name="booking_edit"),
    path("booking/<int:pk>/remove/", views.booking_remove, name="booking_remove"),
    path("booking/<int:pk>/unpin/", views.booking_unpin, name="booking_unpin"),
    path(
        "program/<int:pk>/restore/<int:prescription_pk>/",
        views.booking_restore,
        name="booking_restore",
    ),
    path("groups/", views.group_schedule, name="group_schedule"),
    path("groups/sessions/add/", views.session_add, name="session_add"),
    path("groups/sessions/<int:pk>/edit/", views.session_edit, name="session_edit"),
    path("groups/equipment/<int:pk>/edit/", views.equipment_edit, name="equipment_edit"),
    path("program/<int:pk>/replan/", views.program_replan, name="program_replan"),
    path(
        "program/<int:pk>/pool/<int:prescription_pk>/",
        views.program_pool_group,
        name="program_pool_group",
    ),
    path(
        "program/<int:pk>/instructor/",
        views.program_preferred_instructor,
        name="program_preferred_instructor",
    ),
]
