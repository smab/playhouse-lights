
import random
import sys
import time

from PyQt4 import QtGui, QtCore

import playhouse

usernames = {
    "001788182e78": "25116fda765dc973fae9b4611ec2fb3",
    "00178811f9c2": "newdeveloper",
    "001788182c73": "3PeT4zaFlqOtf2Pr"
}

grid = [
    [("00178811f9c2", 9), ("00178811f9c2", 4), ("00178811f9c2", 7)],
    [("00178811f9c2", 5), ("00178811f9c2", 6), ("00178811f9c2", 8)],
    [("00178811f9c2", 2), ("00178811f9c2", 3), ("00178811f9c2", 1)]
]

ips = {"130.237.228.161:80"}#, "130.237.228.58:80", "130.237.228.213:80"}
buttons = [[None] * 3 for _ in range(3)]

lg = playhouse.LightGrid(usernames, grid, ips, buffered=False)

def main():
    for i in range(3):
        for j in range(3):
            lg.set_state(i, j, sat=0, hue=0, bri=0)
    
    app = QtGui.QApplication(sys.argv)
    
    window = QtGui.QMainWindow()
    window.setWindowTitle("Tic tac toe")
    
    widget = QtGui.QWidget()
    widget.setStyleSheet("QPushButton { color: black }")
    layout = QtGui.QGridLayout()
    
    for row in range(3):
        for column in range(3):
            def clicked(row, column):
                def action():
                    do_turn(column, row)
                return action
            
            button = QtGui.QPushButton("{}:{}".format(row, column))
            button.setSizePolicy(QtGui.QSizePolicy.Expanding, QtGui.QSizePolicy.Expanding)
            button.clicked.connect(clicked(row, column))
            button.setStyleSheet("QPushButton { background-color: white }")
            buttons[row][column] = button
            layout.addWidget(button, row, column)
    
    widget.setLayout(layout)
    window.setCentralWidget(widget)
    
    window.show()
    
    app.exec()


# ====== #

player = 0
colors = [0, 45000]
button_colors = ["red", "blue"]

board = [[-1, -1, -1],
         [-1, -1, -1],
         [-1, -1, -1]]

timer_running = False

def reset():
    global player, board, timer_running
    timer_running = False
    
    for i in range(3):
        for j in range(3):
            lg.set_state(i, j, hue=0, sat=0)
            buttons[j][i].setStyleSheet("QPushButton { background-color: white }")
    board = [[-1, -1, -1],
             [-1, -1, -1],
             [-1, -1, -1]]
    player = 0

def do_turn(x, y):
    global player, timer_running
    if board[y][x] != -1 or timer_running:
        return
    board[y][x] = player
    lg.set_state(x, y, hue=colors[player], sat=255)
    buttons[y][x].setStyleSheet("QPushButton {{ background-color: {} }}".format(button_colors[player]))
    
    winner_lamps = set()
    for configuration in [[(y, 0), (y, 1), (y, 2)],
                          [(0, x), (1, x), (2, x)],
                          [(0, 0), (1, 1), (2, 2)],
                          [(0, 2), (1, 1), (2, 0)]]:
        if all(board[i][j] == player for i, j in configuration):
            winner_lamps.update(configuration)
    
    if len(winner_lamps) > 0:
        def set_alert():
            for i, j in winner_lamps:
                lg.set_state(j, i, alert="lselect")
        QtCore.QTimer.singleShot(500, set_alert)
        timer_running = True
        QtCore.QTimer.singleShot(5000, reset)
        return
    if all(all(i != -1 for i in j) for j in board):
        reset()
        return
    
    player = 1 - player

if __name__ == "__main__":
    main()
