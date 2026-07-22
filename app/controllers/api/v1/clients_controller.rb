module Api
  module V1
    class ClientsController < ApplicationController
      def index
        clients = Client.all
        render json: clients
      end

      def show
        client = Client.find(params[:id])
        render json: client
      rescue ActiveRecord::RecordNotFound
        render json: { error: "Client not found" }, status: :not_found
      end

      def create
        client = Client.new(client_params)

        if client.save
          render json: client, status: :created
        else
          render json: { errors: client.errors }, status: :unprocessable_content
        end
      end

      def update
        client = Client.find(params[:id])
        if client.update(client_params)
          render json: client
        else
          render json: { errors: client.errors }, status: :unprocessable_content
        end
      rescue ActiveRecord::RecordNotFound
        render json: { error: "Client not found" }, status: :not_found
      end

      def destroy
        client = Client.find(params[:id])
        client.destroy
        head :no_content
      rescue ActiveRecord::RecordNotFound
        render json: { error: "Client not found" }, status: :not_found
      end

      private

      def client_params
        params.expect(client: %i[name email])
      end
    end
  end
end
