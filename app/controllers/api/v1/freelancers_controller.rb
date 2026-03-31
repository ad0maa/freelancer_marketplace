module Api
  module V1
    class FreelancersController < ApplicationController
      def index
        freelancers = Freelancer.all
        render json: freelancers
      end

      def show
        freelancer = Freelancer.find(params[:id])
        render json: freelancer
      rescue ActiveRecord::RecordNotFound
        render json: { error: "Freelancer not found" }, status: :not_found
      end

      def create
        freelancer = Freelancer.new(freelancer_params)

        if freelancer.save
          render json: freelancer, status: :created
        else
          render json: { errors: freelancer.errors }, status: :unprocessable_entity
        end
      end

      def update
        freelancer = Freelancer.find(params[:id])
        if freelancer.update(freelancer_params)
          render json: freelancer
        else
          render json: { errors: freelancer.errors }, status: :unprocessable_entity
        end
      rescue ActiveRecord::RecordNotFound
        render json: { error: "Freelancer not found" }, status: :not_found
      end

      def destroy
        freelancer = Freelancer.find(params[:id])
        freelancer.destroy
        head :no_content
      rescue ActiveRecord::RecordNotFound
        render json: { error: "Freelancer not found" }, status: :not_found
      end

      private

      def freelancer_params
        params.expect(freelancer: %i[name email hourly_rate availability])
      end
    end
  end
end
