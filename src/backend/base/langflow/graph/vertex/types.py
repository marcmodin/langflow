import json
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, Iterator, List

import yaml
from langchain_core.messages import AIMessage, AIMessageChunk
from loguru import logger

from langflow.graph.schema import CHAT_COMPONENTS, RECORDS_COMPONENTS, InterfaceComponentTypes
from langflow.graph.utils import UnbuiltObject, serialize_field
from langflow.graph.vertex.base import Vertex
from langflow.schema import Record
from langflow.schema.artifact import ArtifactType
from langflow.schema.schema import INPUT_FIELD_NAME
from langflow.services.monitor.utils import log_transaction, log_vertex_build
from langflow.utils.schemas import ChatOutputResponse, RecordOutputResponse
from langflow.utils.util import unescape_string

if TYPE_CHECKING:
    from langflow.graph.edge.base import ContractEdge


class CustomComponentVertex(Vertex):
    def __init__(self, data: Dict, graph):
        super().__init__(data, graph=graph, base_type="custom_components")

    def _built_object_repr(self):
        if self.artifacts and "repr" in self.artifacts:
            return self.artifacts["repr"] or super()._built_object_repr()


class ComponentVertex(Vertex):
    def __init__(self, data: Dict, graph):
        super().__init__(data, graph=graph, base_type="component")

    def _built_object_repr(self):
        if self.artifacts and "repr" in self.artifacts:
            return self.artifacts["repr"] or super()._built_object_repr()

    def _update_built_object_and_artifacts(self, result):
        """
        Updates the built object and its artifacts.
        """
        if isinstance(result, tuple):
            if len(result) == 2:
                self._built_object, self.artifacts = result
            elif len(result) == 3:
                self._custom_component, self._built_object, self.artifacts = result
        else:
            self._built_object = result

        for key, value in self._built_object.items():
            self.add_result(key, value)

    def get_edge_with_target(self, target_id: str) -> "ContractEdge":
        """
        Get the edge with the target id.

        Args:
            target_id: The target id of the edge.

        Returns:
            The edge with the target id.
        """
        for edge in self.edges:
            if edge.target_id == target_id:
                return edge
        return None

    async def _get_result(self, requester: "Vertex") -> Any:
        """
        Retrieves the result of the built component.

        If the component has not been built yet, a ValueError is raised.

        Returns:
            The built result if use_result is True, else the built object.
        """
        if not self._built:
            log_transaction(source=self, target=requester, flow_id=self.graph.flow_id, status="error")
            raise ValueError(f"Component {self.display_name} has not been built yet")

        if requester is None:
            raise ValueError("Requester Vertex is None")

        edge = self.get_edge_with_target(requester.id)
        if edge is None:
            raise ValueError(f"Edge not found between {self.display_name} and {requester.display_name}")
        if edge.source_handle.name not in self.results:
            raise ValueError(f"Result not found for {edge.source_handle.name}. Results: {self.results}")
        result = self.results[edge.source_handle.name]

        log_transaction(source=self, target=requester, flow_id=self.graph.flow_id, status="success")
        return result


class InterfaceVertex(ComponentVertex):
    def __init__(self, data: Dict, graph):
        super().__init__(data, graph=graph)
        self.steps = [self._build, self._run]

    def build_stream_url(self):
        return f"/api/v1/build/{self.graph.flow_id}/{self.id}/stream"

    def _built_object_repr(self):
        if self.task_id and self.is_task:
            if task := self.get_task():
                return str(task.info)
            else:
                return f"Task {self.task_id} is not running"
        if self.artifacts:
            # dump as a yaml string
            if isinstance(self.artifacts, dict):
                _artifacts = [self.artifacts]
            elif hasattr(self.artifacts, "records"):
                _artifacts = self.artifacts.records
            else:
                _artifacts = self.artifacts
            artifacts = []
            for artifact in _artifacts:
                # artifacts = {k.title().replace("_", " "): v for k, v in self.artifacts.items() if v is not None}
                artifact = {k.title().replace("_", " "): v for k, v in artifact.items() if v is not None}
                artifacts.append(artifact)
            yaml_str = yaml.dump(artifacts, default_flow_style=False, allow_unicode=True)
            return yaml_str
        return super()._built_object_repr()

    def _process_chat_component(self):
        """
        Process the chat component and return the message.

        This method processes the chat component by extracting the necessary parameters
        such as sender, sender_name, and message from the `params` dictionary. It then
        performs additional operations based on the type of the `_built_object` attribute.
        If `_built_object` is an instance of `AIMessage`, it creates a `ChatOutputResponse`
        object using the `from_message` method. If `_built_object` is not an instance of
        `UnbuiltObject`, it checks the type of `_built_object` and performs specific
        operations accordingly. If `_built_object` is a dictionary, it converts it into a
        code block. If `_built_object` is an instance of `Record`, it assigns the `text`
        attribute to the `message` variable. If `message` is an instance of `AsyncIterator`
        or `Iterator`, it builds a stream URL and sets `message` to an empty string. If
        `_built_object` is not a string, it converts it to a string. If `message` is a
        generator or iterator, it assigns it to the `message` variable. Finally, it creates
        a `ChatOutputResponse` object using the extracted parameters and assigns it to the
        `artifacts` attribute. If `artifacts` is not None, it calls the `model_dump` method
        on it and assigns the result to the `artifacts` attribute. It then returns the
        `message` variable.

        Returns:
            str: The processed message.
        """
        artifacts = None
        sender = self.params.get("sender", None)
        sender_name = self.params.get("sender_name", None)
        message = self.params.get(INPUT_FIELD_NAME, None)
        files = [{"path": file} if isinstance(file, str) else file for file in self.params.get("files", [])]
        if isinstance(message, str):
            message = unescape_string(message)
        stream_url = None
        text_output = self.results["message"]
        if isinstance(text_output, (AIMessage, AIMessageChunk)):
            artifacts = ChatOutputResponse.from_message(
                text_output,
                sender=sender,
                sender_name=sender_name,
            )
        elif not isinstance(text_output, UnbuiltObject):
            if isinstance(text_output, dict):
                # Turn the dict into a pleasing to
                # read JSON inside a code block
                message = dict_to_codeblock(text_output)
            elif isinstance(text_output, Record):
                message = text_output.text
            elif isinstance(message, (AsyncIterator, Iterator)):
                stream_url = self.build_stream_url()
                message = ""
                self.results["message"] = message
                self.results["record"].message = message
                self._built_object = self.results
            elif not isinstance(text_output, str):
                message = str(text_output)
            # if the message is a generator or iterator
            # it means that it is a stream of messages
            else:
                message = text_output
            artifact_type = ArtifactType.STREAM if stream_url is not None else ArtifactType.OBJECT
            artifacts = ChatOutputResponse(
                message=message,
                sender=sender,
                sender_name=sender_name,
                stream_url=stream_url,
                files=files,
                type=artifact_type,
            )

            self.will_stream = stream_url is not None
        if artifacts:
            self.artifacts = artifacts.model_dump(exclude_none=True)

        return message

    def _process_record_component(self):
        """
        Process the record component of the vertex.

        If the built object is an instance of `Record`, it calls the `model_dump` method
        and assigns the result to the `artifacts` attribute.

        If the built object is a list, it iterates over each element and checks if it is
        an instance of `Record`. If it is, it calls the `model_dump` method and appends
        the result to the `artifacts` list. If it is not, it raises a `ValueError` if the
        `ignore_errors` parameter is set to `False`, or logs an error message if it is set
        to `True`.

        Returns:
            The built object.

        Raises:
            ValueError: If an element in the list is not an instance of `Record` and
                `ignore_errors` is set to `False`.
        """
        if isinstance(self._built_object, Record):
            artifacts = [self._built_object.data]
        elif isinstance(self._built_object, list):
            artifacts = []
            ignore_errors = self.params.get("ignore_errors", False)
            for record in self._built_object:
                if isinstance(record, Record):
                    artifacts.append(record.data)
                elif ignore_errors:
                    logger.error(f"Record expected, but got {record} of type {type(record)}")
                else:
                    raise ValueError(f"Record expected, but got {record} of type {type(record)}")
        self.artifacts = RecordOutputResponse(records=artifacts)
        return self._built_object

    async def _run(self, *args, **kwargs):
        if self.is_interface_component:
            if self.vertex_type in CHAT_COMPONENTS:
                message = self._process_chat_component()
            elif self.vertex_type in RECORDS_COMPONENTS:
                message = self._process_record_component()
            if isinstance(self._built_object, (AsyncIterator, Iterator)):
                if self.params.get("return_record", False):
                    self._built_object = Record(text=message, data=self.artifacts)
                else:
                    self._built_object = message
            self._built_result = self._built_object

        else:
            await super()._run(*args, **kwargs)

    async def stream(self):
        iterator = self.params.get(INPUT_FIELD_NAME, None)
        if not isinstance(iterator, (AsyncIterator, Iterator)):
            raise ValueError("The message must be an iterator or an async iterator.")
        is_async = isinstance(iterator, AsyncIterator)
        complete_message = ""
        if is_async:
            async for message in iterator:
                message = message.content if hasattr(message, "content") else message
                message = message.text if hasattr(message, "text") else message
                yield message
                complete_message += message
        else:
            for message in iterator:
                message = message.content if hasattr(message, "content") else message
                message = message.text if hasattr(message, "text") else message
                yield message
                complete_message += message
        self.artifacts = ChatOutputResponse(
            message=complete_message,
            sender=self.params.get("sender", ""),
            sender_name=self.params.get("sender_name", ""),
            files=[{"path": file} if isinstance(file, str) else file for file in self.params.get("files", [])],
            type=ArtifactType.OBJECT.value,
        ).model_dump()
        self.params[INPUT_FIELD_NAME] = complete_message
        self._built_object = Record(text=complete_message, data=self.artifacts)
        self._built_result = complete_message
        # Update artifacts with the message
        # and remove the stream_url
        self._finalize_build()
        logger.debug(f"Streamed message: {complete_message}")

        await log_vertex_build(
            flow_id=self.graph.flow_id,
            vertex_id=self.id,
            valid=True,
            params=self._built_object_repr(),
            data=self.result,
            artifacts=self.artifacts,
        )

        self._validate_built_object()
        self._built = True

    async def consume_async_generator(self):
        async for _ in self.stream():
            pass

    def _is_chat_input(self):
        return self.vertex_type == InterfaceComponentTypes.ChatInput and self.is_input


class StateVertex(Vertex):
    def __init__(self, data: Dict, graph):
        super().__init__(data, graph=graph, base_type="custom_components")
        self.steps = [self._build]
        self.is_state = True

    @property
    def successors_ids(self) -> List[str]:
        successors = self.graph.successor_map.get(self.id, [])
        return successors + self.graph.activated_vertices

    def _built_object_repr(self):
        if self.artifacts and "repr" in self.artifacts:
            return self.artifacts["repr"] or super()._built_object_repr()


def dict_to_codeblock(d: dict) -> str:
    serialized = {key: serialize_field(val) for key, val in d.items()}
    json_str = json.dumps(serialized, indent=4)
    return f"```json\n{json_str}\n```"
