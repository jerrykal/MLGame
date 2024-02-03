import abc
import asyncio
import importlib
import json
import os
import time
import traceback

import pandas as pd
import websockets

from mlgame.core.communication import (
    GameCommManager,
    MLCommManager,
    TransitionCommManager,
)
from mlgame.core.exceptions import (
    ErrorEnum,
    GameError,
    GameProcessError,
    MLProcessError,
)
from mlgame.game.generic import quit_or_esc
from mlgame.game.paia_game import PaiaGame
from mlgame.utils.io import save_json
from mlgame.utils.logger import logger
from mlgame.view.view import PygameView, PygameViewInterface


class ExecutorInterface(abc.ABC):
    @abc.abstractmethod
    def run(self):
        pass


class AIClientExecutor(ExecutorInterface):
    def __init__(
        self,
        ai_client_path: str,
        ai_comm: MLCommManager,
        ai_name="1P",
        game_params: dict = {},
    ):
        self._frame_count = 0
        self.ai_comm = ai_comm
        self.ai_path = ai_client_path
        self._proc_name = ai_client_path
        # self._args_for_ml_play = args
        # self._kwargs_for_ml_play = kwargs
        self.ai_name = ai_name
        self.game_params = game_params

    def run(self):
        self.ai_comm.start_recv_obj_thread()
        try:
            module_name = os.path.basename(self.ai_path)
            spec = importlib.util.spec_from_file_location(module_name, self.ai_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            ai_obj = module.MLPlay(ai_name=self.ai_name, game_params=self.game_params)

            # cmd = ai_obj.update({})
            logger.info("             AI Client runs")
            self._ml_ready()
            while True:
                data = self.ai_comm.recv_from_game()
                if data:
                    scene_info, keyboard_info = data
                    if scene_info is None:
                        # game over
                        break
                else:
                    # print(f"ai receive from game : {data}")
                    break

                # assert keyboard_info == "1"
                command = ai_obj.update(scene_info, keyboard_info)
                if scene_info["status"] != "GAME_ALIVE" or command == "RESET":
                    command = "RESET"
                    ai_obj.reset()
                    self._frame_count = 0
                    self._ml_ready()
                    continue
                if command is not None:
                    # 收到資料就回傳
                    self.ai_comm.send_to_game(
                        {"frame": self._frame_count, "command": command}
                    )
                self._frame_count += 1
        # Stop the client of the crosslang module
        except ModuleNotFoundError as e:
            failed_module_name = e.__str__().split("'")[1]
            logger.exception(
                f"Module '{failed_module_name}' is not found in {self._proc_name}"
            )
            exception = MLProcessError(
                self._proc_name,
                "The process '{}' is exited by itself. {}".format(
                    self._proc_name, traceback.format_exc()
                ),
            )
            # send msg to game process
            ai_error = GameError(
                error_type=ErrorEnum.AI_EXEC_ERROR,
                frame=self._frame_count,
                message="The process '{}' is exited by itself. {}".format(
                    self.ai_name, traceback.format_exc()
                ),
            )

            self.ai_comm.send_to_game(ai_error)

        except Exception:
            # handle ai other error
            logger.exception(f"Error is happened in {self._proc_name}")
            exception = MLProcessError(
                self._proc_name,
                "The process '{}' is exited by itself. {}".format(
                    self._proc_name, traceback.format_exc()
                ),
            )
            ai_error = GameError(
                error_type=ErrorEnum.AI_EXEC_ERROR,
                frame=self._frame_count,
                message=f"The process '{self.ai_name}' is exited by itself. {traceback.format_exc()}",
            )

            self.ai_comm.send_to_game(ai_error)
        except SystemExit:  # Catch the exception made by 'sys.exit()'
            print("             System exit at ai client process ")

            ai_error = GameError(
                error_type=ErrorEnum.AI_EXEC_ERROR,
                frame=self._frame_count,
                message=f"The process '{self.ai_name}' is exited by sys.exit. : {traceback.format_exc()}",
            )

            self.ai_comm.send_to_game(ai_error)
        if module == "mlgame.crosslang.ml_play":
            # TODO crosslang
            ai_obj.stop_client()
        print("             AI Client ends")

    def _ml_ready(self):
        """
        Send a "READY" command to the game process
        """
        self.ai_comm.send_to_game("READY")


class GameExecutor(ExecutorInterface):
    def __init__(
        self,
        game: PaiaGame,
        game_comm: GameCommManager,
        game_view: PygameViewInterface,
        fps=30,
        one_shot_mode=False,
        no_display=False,
        output_folder=None,
        wait_action=False,
    ):
        self.wait_action = wait_action
        self._view_data = None
        self._last_pause_btn_clicked_time = 0
        self._pause_state = False
        self.no_display = no_display
        self.game_view = game_view
        self.frame_count = 0
        self.game_comm = game_comm
        self.game = game
        self._active_ml_names = []
        self._ml_delayed_frames = {}
        self._active_ml_names = list(self.game_comm.get_ml_names())
        self._dead_ml_names = []
        self._ml_execution_time = 1 / fps
        self._fps = fps
        self._ml_delayed_frames = {}
        self._output_folder = output_folder
        for name in self._active_ml_names:
            self._ml_delayed_frames[name] = 0
        # self._recorder = get_recorder(self._execution_cmd, self._ml_names)
        self._frame_count = 0
        self._total_frame = 0
        self.one_shot_mode = one_shot_mode
        self._proc_name = str(self.game)

    def run(self):
        game = self.game
        game_view = self.game_view
        try:
            self._send_system_message("AI準備中")
            self._send_game_info(game.get_scene_init_data())
            self._wait_all_ml_ready()
            self._send_system_message("遊戲啟動")
            while not self._quit_or_esc():
                if game_view.is_paused():
                    # 這裡的寫法不太好，但是可以讓遊戲暫停時，可以調整畫面。因為game_view裡面有調整畫面的程式。
                    game_view.draw(self._view_data)
                    time.sleep(0.05)
                    continue
                scene_info_dict = game.get_data_from_game_to_player()
                keyboard_info = game_view.get_keyboard_info()
                cmd_dict = self._make_ml_execute(scene_info_dict, keyboard_info)

                # self._recorder.record(scene_info_dict, cmd_dict)

                result = game.update(cmd_dict)
                self._frame_count += 1
                self._total_frame += 1
                self._view_data = game.get_scene_progress_data()
                game_view.draw(self._view_data)
                # save image
                if self._output_folder:
                    game_view.save_image(
                        f"{self._output_folder}/{self._frame_count:05d}.jpg"
                    )
                self._send_game_progress(self._view_data)

                # Do reset stuff
                if result == "RESET" or result == "QUIT":
                    scene_info_dict = game.get_data_from_game_to_player()
                    # send to ml_clients and don't parse any command , while client reset ,
                    # self._wait_all_ml_ready() will works and not blocks the process
                    for ml_name in self._active_ml_names:
                        self.game_comm.send_to_ml(
                            (scene_info_dict[ml_name], []), ml_name
                        )
                    # TODO check what happen when bigfile is saved
                    time.sleep(0.1)
                    game_result = game.get_game_result()

                    attachments = game_result["attachment"]
                    print(pd.DataFrame(attachments).to_string())

                    if self.one_shot_mode or result == "QUIT":
                        self._send_system_message("遊戲結束")
                        self._send_game_result(game_result)
                        if self._output_folder:
                            save_json(self._output_folder, game_result)
                        self._send_system_message("關閉遊戲")
                        self._send_end_message()
                        time.sleep(1)

                        break

                    game.reset()
                    game_view.reset()

                    self._frame_count = 0
                    # TODO think more
                    for name in self._active_ml_names:
                        self._ml_delayed_frames[name] = 0
                    self._wait_all_ml_ready()
        except Exception:
            # handle unknown exception
            # send to es
            e = GameProcessError(self._proc_name, traceback.format_exc())
            logger.exception("Some errors happened in game process.")
            self._send_game_error_with_obj(
                GameError(
                    error_type=ErrorEnum.GAME_EXEC_ERROR,
                    message=e.__str__(),
                    frame=self._frame_count,
                )
            )

        pass

    def _wait_all_ml_ready(self):
        """
        Wait until receiving "READY" commands from all ml processes
        """
        # Wait the ready command one by one
        for ml_name in self._active_ml_names:
            recv = ""
            while recv != "READY":
                try:
                    recv = self.game_comm.recv_from_ml(ml_name)
                    if isinstance(recv, GameError):
                        # handle error when ai_client couldn't be ready state.
                        # logger.info(recv.message)
                        self._dead_ml_names.append(ml_name)
                        self._active_ml_names.remove(ml_name)
                        self._send_game_error_with_obj(recv)
                        break
                except Exception as e:
                    print("catch error 2")
                    self._dead_ml_names.append(ml_name)
                    self._active_ml_names.remove(ml_name)
                    # self._send_game_error(f"AI of {ml_name} has error at initial stage.")
                    ai_error = GameError(
                        error_type=ErrorEnum.AI_INIT_ERROR,
                        frame=0,
                        message=f"AI of {ml_name} has error at initial stage. {e.__str__()}",
                    )

                    self._send_game_error_with_obj(ai_error)
                    break

    def _make_ml_execute(self, scene_info_dict, keyboard_info) -> dict:
        """
        Send the scene information to all ml processes and wait for commands

        @return A dict of the recevied command from the ml clients
                If the client didn't send the command, it will be `None`.
        """
        try:
            for ml_name in self._active_ml_names:
                self.game_comm.send_to_ml(
                    (scene_info_dict[ml_name], keyboard_info), ml_name
                )
        except KeyError:
            raise KeyError(
                "The game doesn't provide scene information "
                f"for the client '{ml_name}'"
            )

        if not self.wait_action:
            time.sleep(self._ml_execution_time)
        response_dict = self.game_comm.recv_from_all_ml()

        cmd_dict = {}
        for ml_name in self._active_ml_names[:]:
            cmd_received = response_dict[ml_name]
            if isinstance(cmd_received, MLProcessError):
                # print(cmd_received.message)
                # handle error from ai clients
                self._send_game_error_with_obj(
                    GameError(
                        error_type=ErrorEnum.AI_EXEC_ERROR,
                        message=str(cmd_received),
                        frame=self._frame_count,
                    )
                )
                self._dead_ml_names.append(ml_name)
                self._active_ml_names.remove(ml_name)
            elif isinstance(cmd_received, GameError):
                self._send_game_error_with_obj(cmd_received)
                self._dead_ml_names.append(ml_name)
                self._active_ml_names.remove(ml_name)
            elif isinstance(cmd_received, dict):
                self._check_delay(ml_name, cmd_received["frame"])
                cmd_dict[ml_name] = cmd_received["command"]
            else:
                cmd_dict[ml_name] = None

        for ml_name in self._dead_ml_names:
            cmd_dict[ml_name] = None

        if len(self._active_ml_names) == 0:
            error = MLProcessError(
                self._proc_name,
                f"The process {self._proc_name} exit because all ml processes has exited.",
            )
            game_error = GameError(
                error_type=ErrorEnum.GAME_EXEC_ERROR,
                frame=self._frame_count,
                message="All ml clients has been terminated",
            )

            self._send_game_error_with_obj(game_error)
            self._send_game_result(self.game.get_game_result())
            self._send_end_message()

            raise error
        return cmd_dict

    def _check_delay(self, ml_name, cmd_frame):
        """
        Check if the timestamp of the received command is delayed
        """
        delayed_frame = self._frame_count - cmd_frame
        if delayed_frame > self._ml_delayed_frames[ml_name]:
            self._ml_delayed_frames[ml_name] = delayed_frame
            print(
                f"The client '{ml_name}' delayed {delayed_frame} frame at frame({self._frame_count})"
            )

    def _quit_or_esc(self) -> bool:
        if self.no_display:
            return self._frame_count > 30000
        else:
            return quit_or_esc()

    def _send_game_result(self, game_result_dict):
        # TO be deprecated("_send_game_error_with_obj")
        data_dict = {"type": "game_result", "data": game_result_dict}
        self.game_comm.send_to_others(data_dict)

    def _send_system_message(self, msg: str):
        data_dict = {"type": "system_message", "data": {"message": msg}}
        self.game_comm.send_to_others(data_dict)

    def _send_game_info(self, game_info_dict):
        data_dict = {"type": "game_info", "data": game_info_dict}
        self.game_comm.send_to_others(data_dict)

    def _send_game_progress(self, game_progress_dict):
        """
        Send the game progress to the transition server
        """
        game_progress_dict["frame"] = self._total_frame
        data_dict = {"type": "game_progress", "data": game_progress_dict}

        self.game_comm.send_to_others(data_dict)
        # print(data_dict)

    def _send_game_error(self, system_message):
        # TO be deprecated("_send_game_error_with_obj")
        data_dict = {"type": "game_error", "data": {"message": system_message}}

        self.game_comm.send_to_others(data_dict)

    def _send_game_error_with_obj(self, error: GameError):
        # print(error)
        data_dict = {
            "type": "game_error",
            "data": {
                "message": error.message,
                "error_type": error.error_type.name,
                "frame": error.frame,
            },
        }
        self.game_comm.send_to_others(data_dict)

        # self._send_game_error(data_dict)

    def _send_end_message(self):
        self.game_comm.send_to_others(None)


class GameManualExecutor(ExecutorInterface):
    # TODO deprecated
    def __init__(
        self,
        game: PaiaGame,
        game_view: PygameViewInterface,
        game_comm: GameCommManager,
        fps=30,
        one_shot_mode=False,
    ):
        self.game_view = game_view
        self.frame_count = 0
        self.game = game
        self.game_comm = game_comm
        self._ml_delayed_frames = {}
        self._ml_execution_time = 1 / fps
        self._fps = fps
        self._ml_delayed_frames = {}
        # self._recorder = get_recorder(self._execution_cmd, self._ml_names)
        self._frame_count = 0
        self.one_shot_mode = one_shot_mode
        self._proc_name = self.game.__class__.__str__

    def run(self):
        game = self.game
        game_view = self.game_view
        self.game_comm.send_to_others(game_view.scene_init_data)

        try:
            while not quit_or_esc():
                cmd_dict = game.get_keyboard_command()
                # self._recorder.record(scene_info_dict, cmd_dict)
                result = game.update(cmd_dict)
                self._frame_count += 1
                view_data = game.get_scene_progress_data()
                self.game_comm.send_to_others(view_data)
                game_view.draw(view_data)
                if not self.wait_action:
                    time.sleep(self._ml_execution_time)
                # Do reset stuff
                if result == "RESET" or result == "QUIT":
                    game_result = game.get_game_result()
                    attachments = game_result["attachment"]
                    print(pd.DataFrame(attachments).to_string())
                    if self.one_shot_mode or result == "QUIT":
                        self.game_comm.send_to_others(game_result)
                        break
                    game.reset()
                    game_view.reset()
                    # self._frame_count = 0

        except Exception as e:
            # handle unknown exception
            # send to es
            logger.exception(f"Some errors happened in game process. {e.__str__()}")
        logger.info("pingpong end.")


class ProgressLogExecutor(ExecutorInterface):
    def __init__(
        self, progress_folder, progress_frame_frequency, pl_comm: TransitionCommManager
    ):
        # super().__init__(name="ws")
        self._proc_name = f"progress_log({progress_folder}"
        self._progress_folder = progress_folder
        self._progress_frame_frequency = progress_frame_frequency
        self._comm_manager = pl_comm
        self._recv_data_func = self._comm_manager.recv_from_game
        self._filename = "{}.json"
        self._progress_data = []

    def save_json_and_init(self, path):
        print(path)
        with open(path, "w") as f:
            json.dump(self._progress_data, f)
        self._progress_data = []

    def run(self):
        self._comm_manager.start_recv_obj_thread()

        try:
            progress_count = -1
            while (game_data := self._recv_data_func())["type"] != "game_result":
                if game_data["type"] == "game_progress":
                    # print(game_data)
                    if (
                        game_data["data"]["frame"] - 1
                    ) % self._progress_frame_frequency == 0 and game_data["data"][
                        "frame"
                    ] != 1:
                        self.save_json_and_init(
                            os.path.join(
                                self._progress_folder,
                                self._filename.format(
                                    progress_count := progress_count + 1
                                ),
                            )
                        )
                    self._progress_data.append(game_data["data"])
            else:
                if self._progress_data != []:
                    self.save_json_and_init(
                        os.path.join(
                            self._progress_folder,
                            self._filename.format(
                                str(progress_count := progress_count + 1) + "-end"
                            ),
                        )
                    )
        except Exception as e:
            # exception = TransitionProcessError(self._proc_name, traceback.format_exc())
            self._comm_manager.send_exception(f"exception on {self._proc_name}")
            # catch connection error
            print("except", e)
        finally:
            print("end pl")


class WebSocketExecutor:
    def __init__(self, ws_uri, ws_comm: TransitionCommManager):
        # super().__init__(name="ws")
        logger.info("             ws_init ")
        self._proc_name = f"websocket({ws_uri}"
        self._ws_uri = ws_uri
        self._comm_manager = ws_comm
        self._recv_data_func = self._comm_manager.recv_from_game

    async def ws_start(self):
        async with websockets.connect(self._ws_uri, ping_interval=None) as websocket:
            logger.info("             ws_start")
            count = 0
            is_ready_to_end = False
            while 1:
                data = self._recv_data_func()
                if data is None:
                    print("ws received from game:", data)
                    break
                elif isinstance(data, GameError):
                    print("ws received :", data)
                    await websocket.send(data.data())
                    # exit container
                    if data.error_type in [
                        ErrorEnum.COMMAND_ERROR,
                        ErrorEnum.GAME_EXEC_ERROR,
                    ]:
                        await websocket.send(
                            {
                                "type": "system_message",
                                "data": {"message": f"error in {data.error_type}"},
                            }
                        )
                        break
                        # os.system("pgrep -f 'tail -f /dev/null' | xargs kill")
                elif isinstance(data, MLProcessError):
                    print("ws received :", data)
                    # await websocket.send(data.data())
                    # exit container
                    # if data.error_type in [ErrorEnum.COMMAND_ERROR, ErrorEnum.GAME_EXEC_ERROR]:
                    await websocket.send(
                        {
                            "type": "system_message",
                            "data": {"message": f"error in {data.message}"},
                        }
                    )
                    break
                elif data["type"] == "game_result":
                    # raise a flag to recv data
                    is_ready_to_end = True
                    await websocket.send(json.dumps(data))
                else:
                    print(data)
                    await websocket.send(json.dumps(data))
                    # count += 1
                    pass
                    # print(f'Send to ws : {count}:{data.keys()}')
                    #
                # make sure webservice got game result then mlgame is able to close websocket

            while is_ready_to_end:
                # wait for game_result
                ws_recv_data = await websocket.recv()
                print("ws received from django:", ws_recv_data)
                if ws_recv_data == "game_result":
                    print(f"< {ws_recv_data}")
                    await websocket.close()
                    is_ready_to_end = False

                    # else:
                    #     await websocket.send(json.dumps({}))
            # time.sleep(1)
            # await websocket.close()

    def run(self):
        self._comm_manager.start_recv_obj_thread()
        try:
            asyncio.get_event_loop().run_until_complete(self.ws_start())
        except Exception as e:
            # exception = TransitionProcessError(self._proc_name, traceback.format_exc())
            self._comm_manager.send_exception(f"exception on {self._proc_name}")
            # catch connection error
            logger.exception(e.__str__())
        finally:
            print("end ws ")


class DisplayExecutor(ExecutorInterface):
    def __init__(self, display_comm: TransitionCommManager, scene_init_data):
        # super().__init__(name="ws")
        logger.info("             display_process_init ")
        self._proc_name = "display"
        self._comm_manager = display_comm
        self._recv_data_func = self._comm_manager.recv_from_game
        self._scene_init_data = scene_init_data

    def run(self):
        self.game_view = PygameView(self._scene_init_data)
        self._comm_manager.start_recv_obj_thread()
        try:
            while (game_data := self._recv_data_func())["type"] != "game_result":
                if game_data["type"] == "game_progress":
                    # print(game_data)
                    self.game_view.draw(game_data["data"])
                    pass
        except Exception as e:
            # exception = TransitionProcessError(self._proc_name, traceback.format_exc())
            self._comm_manager.send_exception(f"exception on {self._proc_name}")
            # catch connection error
            print("except", e)
        finally:
            print("end display process")
