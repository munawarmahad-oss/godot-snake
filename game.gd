extends Node2D

const CELL_SIZE := 32
const GRID_WIDTH := 20
const GRID_HEIGHT := 15

const BG_COLOR := Color(0.08, 0.08, 0.08)
const SNAKE_HEAD_COLOR := Color(0.2, 0.9, 0.2)
const SNAKE_BODY_COLOR := Color(0.1, 0.7, 0.1)
const FOOD_COLOR := Color(0.95, 0.2, 0.2)
const GRID_COLOR := Color(0.16, 0.16, 0.16)
const GAME_OVER_COLOR := Color(1.0, 1.0, 1.0)

var snake: Array[Vector2i] = []
var direction := Vector2i.RIGHT
var next_direction := Vector2i.RIGHT
var food := Vector2i.ZERO
var score := 0
var game_over := false
var rng := RandomNumberGenerator.new()

@onready var move_timer: Timer = $MoveTimer
@onready var camera: Camera2D = $Camera2D


func _ready() -> void:
	rng.randomize()
	_setup_camera()
	_start_game()
	move_timer.timeout.connect(_on_move_timer_timeout)


func _setup_camera() -> void:
	var center_x := GRID_WIDTH * CELL_SIZE / 2.0
	var center_y := GRID_HEIGHT * CELL_SIZE / 2.0
	camera.position = Vector2(center_x, center_y)


func _start_game() -> void:
	score = 0
	game_over = false

	snake.clear()
	snake.append(Vector2i(8, 7))
	snake.append(Vector2i(7, 7))
	snake.append(Vector2i(6, 7))

	direction = Vector2i.RIGHT
	next_direction = Vector2i.RIGHT

	_spawn_food()
	move_timer.start()
	queue_redraw()


func _input(event: InputEvent) -> void:
	if event.is_action_pressed("ui_up") and direction != Vector2i.DOWN:
		next_direction = Vector2i.UP
	elif event.is_action_pressed("ui_down") and direction != Vector2i.UP:
		next_direction = Vector2i.DOWN
	elif event.is_action_pressed("ui_left") and direction != Vector2i.RIGHT:
		next_direction = Vector2i.LEFT
	elif event.is_action_pressed("ui_right") and direction != Vector2i.LEFT:
		next_direction = Vector2i.RIGHT
	elif event.is_action_pressed("ui_accept") and game_over:
		_start_game()


func _on_move_timer_timeout() -> void:
	if game_over:
		return

	direction = next_direction
	var new_head := snake[0] + direction

	if new_head.x < 0 or new_head.x >= GRID_WIDTH or new_head.y < 0 or new_head.y >= GRID_HEIGHT:
		_end_game()
		return

	if new_head in snake:
		_end_game()
		return

	snake.push_front(new_head)

	if new_head == food:
		score += 1
		_spawn_food()
	else:
		snake.pop_back()

	queue_redraw()


func _spawn_food() -> void:
	var empty_cells: Array[Vector2i] = []

	for y in range(GRID_HEIGHT):
		for x in range(GRID_WIDTH):
			var cell := Vector2i(x, y)
			if cell not in snake:
				empty_cells.append(cell)

	if empty_cells.is_empty():
		_end_game()
		return

	food = empty_cells[rng.randi_range(0, empty_cells.size() - 1)]


func _end_game() -> void:
	game_over = true
	move_timer.stop()
	queue_redraw()


func _draw() -> void:
	draw_rect(Rect2(Vector2.ZERO, Vector2(GRID_WIDTH * CELL_SIZE, GRID_HEIGHT * CELL_SIZE)), BG_COLOR, true)

	for x in range(GRID_WIDTH + 1):
		var x_pos := x * CELL_SIZE
		draw_line(
			Vector2(x_pos, 0),
			Vector2(x_pos, GRID_HEIGHT * CELL_SIZE),
			GRID_COLOR,
			1.0
		)

	for y in range(GRID_HEIGHT + 1):
		var y_pos := y * CELL_SIZE
		draw_line(
			Vector2(0, y_pos),
			Vector2(GRID_WIDTH * CELL_SIZE, y_pos),
			GRID_COLOR,
			1.0
		)

	_draw_cell(food, FOOD_COLOR)

	for i in range(snake.size()):
		if i == 0:
			_draw_cell(snake[i], SNAKE_HEAD_COLOR)
		else:
			_draw_cell(snake[i], SNAKE_BODY_COLOR)

	draw_string(
		ThemeDB.fallback_font,
		Vector2(10, 24),
		"Score: %d" % score,
		HORIZONTAL_ALIGNMENT_LEFT,
		-1,
		20
	)

	if game_over:
		var msg := "Game Over - Press Enter to Restart"
		draw_string(
			ThemeDB.fallback_font,
			Vector2(60, GRID_HEIGHT * CELL_SIZE / 2.0),
			msg,
			HORIZONTAL_ALIGNMENT_LEFT,
			-1,
			24,
			GAME_OVER_COLOR
		)


func _draw_cell(cell: Vector2i, color: Color) -> void:
	var pos := Vector2(cell.x * CELL_SIZE, cell.y * CELL_SIZE)
	draw_rect(
		Rect2(pos + Vector2(2, 2), Vector2(CELL_SIZE - 4, CELL_SIZE - 4)),
		color,
		true
	)
